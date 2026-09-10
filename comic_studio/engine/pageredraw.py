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


def save_kf_version(shot_dir, role: str, src: Path) -> str:
    """新版本落盘（v{max+1}）并刷新活动拷贝。返回 "vN"。"""
    d = Path(shot_dir); d.mkdir(parents=True, exist_ok=True)
    n = 0
    for v in kf_versions(d, role):
        n = max(n, int(v[1:]))
    data = Path(src).read_bytes()
    (d / f"kf_{role}_v{n + 1}.png").write_bytes(data)
    (d / f"kf_{role}.png").write_bytes(data)
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
    prompt = ("高质量重绘这张漫画页：保持原有分格构图、人物姿态、位置与表情"
              "与参考图完全一致，仅提升画质清晰度、光影与细节")
    # 角色外貌文字锚（首个绑定角色；IP-Adapter 图像锚走工作流 char_ref 槽）
    led = json.loads(shot["ledger_json"] or "{}")
    for aid in (led.get("assets", {}) or {}).get("characters", []):
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
    return prompt + PAGE_TAIL


def redraw_page(db, data_dir, shot_id, comfy, job=None, seed=None) -> Path:
    """整页重绘一镜首帧：base=原页 → 新版本 → 激活 → 前镜尾帧联动 → 视频置空。
    模板=template_map.page_redraw（决策：默认 zimage_i2i base 槽；模板若声明
    第二图槽则注入该镜首个绑定角色 main.png——条件双槽）。"""
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
    images = [{"slot": tmpl.inject_images[0]["slot"], "path": str(base)}]
    anchor_line = ("。人物与背景的布局、构图与原页保持一致，仅画风与质感按提示词转换")
    if len(tmpl.inject_images or []) >= 2:   # 条件双槽：角色主图身份锚（IP-Adapter）
        led = json.loads(shot["ledger_json"] or "{}")
        from .assets import get_asset
        from .paths import data_to_abs
        for aid in (led.get("assets", {}) or {}).get("characters", []):
            a = get_asset(db, aid)
            if a and a["library_dir"]:
                main = data_to_abs(data_dir, a["library_dir"]) / "main.png"
                if main.exists():
                    images.append({"slot": tmpl.inject_images[1]["slot"],
                                   "path": str(main)})
                    # 主图入槽 → 提示词同步声明身份一致（用户 _raw 实测用语）
                    anchor_line += "。人物的五官、发型与体态与角色参考图保持一致"
            break
    seed = seed if seed is not None else _video_seed(shot)
    prompt = build_page_redraw_prompt(db, proj, shot) + anchor_line
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": seed,
                "denoise": (get_setting(db, "comfy") or {}).get("page_redraw_denoise", 0.55)},
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


def enqueue_batch_redraw(db, data_dir, project_id) -> int:
    """「批量重绘分镜」：标记 pending_redraw → 逐镜入队 gpu_comfy（决策 11 逐镜
    job：温和停止可停/单镜失败不殃及）→ redraw_done=1。
    统一零-pending 规则（终审 2026-09-09）：当前没有任何镜带 pending 标记 →
    视为全新整批（全部非禁用镜重新标记+入队，不论 redraw_done——purge→重提取
    →重批量 与 换画风→整批重做 的正当入口，前端有确认框守护）；仍有镜带标记
    → 补漏（只入队带标记的镜，已完成镜不重复）。批次在飞中重发：在飞镜的
    标记未清，落补漏路径，只对未提交的镜补队，无整批重复。"""
    from .jobs import enqueue_job
    from .llm.storyboard import auto_bind_characters
    from .settings import ensure_comfy_configured
    import random
    require_redraw_project(db, project_id)
    ensure_comfy_configured(db)   # 409 门禁由路由转 HTTPException
    # F2b（2026-09-10 真机四修）：入口补跑补绑——存量项目「强制重读」统一命名后
    # 点批量即自动绑上（真机根因①：读图 VLM 与提取 VLM 各起各名，绑定全空）
    auto_bind_characters(db, project_id)
    shots = [s for s in list_shots(db, project_id) if not s["disabled"]]

    def _pending(s) -> bool:
        try:
            return bool(json.loads(s["ledger_json"] or "{}").get("pending_redraw"))
        except (ValueError, TypeError):
            return False

    fresh = not any(_pending(s) for s in shots)   # 零 pending = 全新整批
    batch_seed = random.randint(0, 2 ** 31 - 1)   # F3：同批同 seed（根因②风格漂移）
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
