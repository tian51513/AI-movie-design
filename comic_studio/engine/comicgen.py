# comic_studio/engine/comicgen.py
"""小说转漫画逐页生成（2026-09-12 设计定稿）：gen_comic_page 逐镜 t2i 出页。

链路：comic 分镜（Task 2，workflow_type='comic'）→ 本 handler 逐页提交
comic_page 模板（默认 Z-Image Turbo 文生图，尺寸/步数按项目列）→
落盘 projects/<slug>/pages/page_NNN.png → 对白后处理（bubble=提示词层
画气泡，footer=Pillow 底部字幕条，none=不呈现）→ shot 标 comic_ready。
"""
import json
import random
from pathlib import Path

from .logbus import emit as emit_log
from .queue.worker import register
from .shots import get_shot, update_shot

# 质量档位 → 采样步数
QUALITY_STEPS = {"fast": 8, "standard": 12, "high": 20}

# 百万像素档（2026-09-13 用户决策：项目尺寸设置=百万像素，输出结构随模板
# 原生走——Krea2 编辑模式跟输入图形状，纯 t2i 按 MP×项目画幅推导）
MP_TIERS = ["0.3", "0.4", "0.6", "0.8", "1.0", "1.2", "1.6"]

# 旧 WxH 预设（存量值兼容换算用）
SIZE_PRESETS = {
    "512x512": (512, 512), "512x768": (512, 768), "768x512": (768, 512),
    "768x768": (768, 768), "768x1024": (768, 1024), "1024x768": (1024, 768),
    "832x1216": (832, 1216), "1024x1024": (1024, 1024),
    "1024x1536": (1024, 1536), "1536x1024": (1536, 1024),
}


def parse_image_mp(image_size: str) -> float:
    """image_size → 百万像素。新值 '0.4' 直读；旧 WxH 换算；兜 1.0。"""
    v = (image_size or "").strip().lower().rstrip("mp")
    try:
        return max(0.1, min(4.0, float(v)))
    except ValueError:
        pass
    if image_size in SIZE_PRESETS:
        w, h = SIZE_PRESETS[image_size]
        return round(w * h / 1e6, 2)
    return 1.0


def mp_to_wh(mp: float, aspect_ratio: str) -> tuple[int, int]:
    """MP × 画幅（a:b）→ (width, height)，32 对齐（t2i latent 兼容）。"""
    try:
        aw, ah = (int(x) for x in (aspect_ratio or "9:16").split(":"))
    except ValueError:
        aw, ah = 9, 16
    total = mp * 1e6
    w = (total * aw / ah) ** 0.5
    h = w * ah / aw
    return (max(64, int(round(w / 32)) * 32), max(64, int(round(h / 32)) * 32))


def _ensure_blank(data_dir, name="blank_ref.png", rgb=(255, 255, 255)) -> str:
    """占位图（缺省参考槽用）：data/_cache/<name>，无则生成 64x64。
    v1.4：场景/第二角色缺省槽用中性灰（blank_gray.png）——白图偏亮会把
    「新场景」带偏户外（真机判例：室内卧室出成室外）。"""
    import struct
    import zlib
    d = Path(data_dir) / "_cache"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if not p.exists():
        w = h = 64
        px = bytes(rgb)
        raw = b"".join(b"\x00" + px * w for _ in range(h))
        def chunk(tag, data):
            c = struct.pack(">I", len(data)) + tag + data
            return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        p.write_bytes(png)
    return str(p)


def _ledger(shot) -> dict:
    try:
        return json.loads(shot["ledger_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _stitch_mains(data_dir, paths) -> str:
    """双角色主图左右拼接（等高）进人物槽——用户定案方案（2026-09-13）：
    2角色+场景时拼两人为一张参考（图2），场景进图1；提示词说明左右身份。
    mtime 哈希缓存（主图不变即复用）；无 Pillow/图损退第一张。"""
    import hashlib
    key = hashlib.md5("|".join(f"{p}:{Path(p).stat().st_mtime_ns}"
                               for p in map(str, paths)).encode()).hexdigest()[:12]
    out = Path(data_dir) / "_cache" / f"stitch_{key}.png"
    if out.exists():
        return str(out)
    try:
        from PIL import Image
        imgs = [Image.open(p).convert("RGB") for p in paths]
        hgt = min(i.height for i in imgs)
        rs = [i.resize((max(1, int(i.width * hgt / i.height)), hgt)) for i in imgs]
        canvas = Image.new("RGB", (sum(i.width for i in rs), hgt), (128, 128, 128))
        x = 0
        for i in rs:
            canvas.paste(i, (x, 0))
            x += i.width
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out)
        return str(out)
    except Exception:
        return str(paths[0])


def _anchor_detail(raw: str) -> str:
    """appearance_json 可能是 {"detail": 行模板} 包装（2026-09-13 真机判例：
    不拆包整段 JSON 泄漏进提示词成噪声）——拆包失败回落原文。"""
    try:
        d = json.loads(raw or "")
        if isinstance(d, dict):
            return str(d.get("detail") or "")
    except (ValueError, TypeError):
        pass
    return raw or ""


def _anchor_lines(shot, db, project_id) -> str:
    """文本外貌锚（2026-09-13 一致性）：绑定角色的资产库外貌行 + 场景描述
    注入页面提示词——与参考图生成同源数据，跨页文字锚定（一期无参考图注入，
    同一角色每页靠这段保持发型/服装一致）。上限 3 角色/2 场景防爆。"""
    led = _ledger(shot)
    bound = led.get("assets") or {}

    from .assets import get_asset
    parts = []
    for cid in (bound.get("characters") or [])[:3]:
        a = get_asset(db, cid)
        if a is None or a["kind"] != "character":
            continue
        rows = [ln.strip() for ln in _anchor_detail(a["appearance_json"]).splitlines()
                if ln.strip()]
        rows = [ln for ln in rows if not ln.endswith("无")]  # 「无」行零信息
        if rows:
            parts.append(f"{a['name']}（{'；'.join(rows)}）")
    for sid in (bound.get("scenes") or [])[:2]:
        a = get_asset(db, sid)
        if a is not None and a["kind"] == "scene":
            d = _anchor_detail(a["appearance_json"])
            if d:
                parts.append(f"场景{a['name']}：{d[:60]}")
    return "；".join(parts)


def _prev_page_summary(db, shot) -> str:
    """上一页（seq-1，生效镜）的动作/姿势摘要——页间连续性注入用
    （2026-09-13 用户需求：坐着→躺着突兀跳变）。"""
    row = db.connect().execute(
        "SELECT description FROM shots WHERE project_id=? AND seq=? AND disabled=0",
        (shot["project_id"], shot["seq"] - 1)).fetchone()
    return (row["description"] or "").strip()[:80] if row else ""


def build_comic_prompt(db, proj, shot, scene_mode=False, extra_chars=None,
                       scene_img=False, scene_name="", krea_names=None,
                       second_anchor="", prev_summary="", continuity="") -> str:
    """漫画页提示词：场景 + 外貌锚 + 画风。

    对白一律不进提示词（2026-09-13 气泡渲染反转：t2i 画中文=乱码）——
    bubble 模式注入「上方留白+禁字」指令，气泡由 Pillow 后处理画；
    footer 的对白由后处理字幕条呈现，none 直接丢弃。

    scene_mode（二期参考注入 2026-09-13）：base=角色主图时改人物场景化指令
    ——「将图中人物置于{场景}」+ 面部发型服装保持一致（Edit-2511 训练分布）；
    extra_chars=base 之外仍需出现的角色（文字锚）。
    """
    import re as _re
    # 剥拆解绑定引用「（id=N）」——对图像模型是无意义 token（2026-09-13 真机泄漏）
    detail = _re.sub(r"[（(]id=\d+[）)]", "",
                     (shot["description"] or "").strip()) or "按分镜描述生成"
    style = ((proj["style_vis"] or proj["style"]) or "").strip()
    if scene_mode:
        # 参考路径：身份由图锚定，文字锚降为名字映射（全行锚与「保持一致」
        # 指令重复堆叠，挤占分镜场景与画风遵循——2026-09-13 真机判例）
        who = "图1中的人物" if not extra_chars else f"图1中的人物与图2中的人物（{extra_chars}）"
        where = "图3中的场景" if scene_img else "新场景"
        scene_anchor = f"场景：{scene_name}，" if scene_name else ""
        if krea_names:
            # Krea2 快道方言：图1=场景 / 图2=人物（槽序固定）。单次出图
            # （用户决策 2026-09-13：双角色不做链式两段——第一人入图2 参考，
            # 第二人文字锚）
            if len(krea_names) == 2 and scene_img:
                # 拼接模式：图2=两人合成参考
                who = (f"图2 参考中的两位人物（左侧是{krea_names[0]}，"
                       f"右侧是{krea_names[1]}），两人外貌与图2 保持一致")
            elif len(krea_names) == 2:
                # 2角色+0场景直种：图1=第二人物参考（无场景图时占场景槽）
                who = (f"图2 参考中的人物（{krea_names[0]}）与图1 参考中的人物"
                       f"（{krea_names[1]}），两人外貌分别与各自参考图保持一致")
            else:
                who = f"图2 参考中的人物（{krea_names[0]}），外貌与图2 保持一致"
            where = "图1 的场景" if scene_img else "按描述构建的场景"
            prompt = (f"{scene_anchor}画面内容：{detail[:200]}。"
                      f"将{who}置于{where}。"
                      "画面构图（全身/半身/特写/机位）严格按描述执行。"
                      "场所与光线严格按描述（室内就是室内，室外就是室外），"
                      "画面高清锐利、细节清晰")
        else:
            prompt = (f"严格按场景描述重新构图：{scene_anchor}{detail[:200]}。"
                      f"将{who}置于{where}，人物五官/发型/服装与参考图保持一致。"
                      "画面构图（全身/半身/特写/机位）严格按描述执行，人物完整按描述呈现。"
                      "场所与光线严格按描述（室内就是室内，室外就是室外），"
                      "画面高清锐利、细节清晰")
        if style:
            prompt += f"。画风（严格执行）：{style}"   # 画风前置加重（尾部被淹没判例）
    else:
        prompt = f"单格漫画插画：{detail[:200]}"
        anchor = _anchor_lines(shot, db, proj["id"])
        if anchor:
            prompt += f"。角色外貌锚（各角色严格保持一致）：{anchor}"
        if style:
            prompt += f"。画风：{style}"
    # 页间连续性（2026-09-13 用户需求：前后页姿势/构图突变）：continuity∈
    # {全程继承,微变延续} 的页注入上一页姿势摘要——模型知道上一格人物什么
    # 姿态，本页在描述变化之外保持衔接；场景断点页不注入（合法跳变）
    if prev_summary and continuity in ("全程继承", "微变延续"):
        prompt += (f"。与上一页画面连续：上一格{prev_summary}——"
                   "人物姿势与位置在此基础上衔接变化，不要突兀跳变")
    if (proj["dialogue_mode"] or "bubble") == "bubble":
        prompt += "。画面上方适当留白，不要画任何文字、对话气泡、字幕"
    return prompt


@register("gen_comic_page")
def handle_gen_comic_page(db, data_dir, job, comfy):
    """逐页 t2i 生成 → pages/page_NNN.png → 对白后处理 → shot 标 comic_ready。"""
    from .jobs import attach_snapshot
    from .projects import get_project
    from .settings import get_setting
    from .workflows.filler import fill_workflow
    from .workflows.registry import resolve_template

    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    if shot is None:
        raise ValueError("分镜已删除（gen_comic_page）")
    if comfy is None:
        raise ValueError("ComfyUI 未配置地址（gen_comic_page 需 ComfyUI）")
    proj = get_project(db, shot["project_id"])
    tmpl = resolve_template(db, "comic_page")

    mp = parse_image_mp(proj["image_size"])
    w, h = mp_to_wh(mp, proj["aspect_ratio"])   # 纯 t2i 用（krea 道直传 MP）
    # seed 库值优先（2026-09-13 页间连续性③：拆解时延续组 +3 已算好——同组页
    # 渲染风格/人物样貌更稳；无库值才随机）
    _seed = shot["seed"] if "seed" in shot.keys() and shot["seed"] else None
    dual_mode = ((proj["comic_dual_mode"] if "comic_dual_mode" in proj.keys()
                  else "") or "stitch")          # stitch 默认 / chain 链式
    steps = QUALITY_STEPS.get(proj["quality_tier"] or "standard", 12)
    params = {"seed": _seed if _seed is not None else random.randint(0, 2**31 - 1),
              "steps": steps, "width": w, "height": h}
    images = []
    # 二期参考注入分流（2026-09-13）：绑定角色 ≤2 且有 main.png → 人物场景化
    # 模板（base=第一角色主图；第二角色文字锚）。无绑定/主图缺/>2 → 纯 t2i。
    from .assets import get_asset
    from .paths import data_to_abs as _dta
    bound = _ledger(shot).get("assets") or {}
    char_refs = []
    for cid in (bound.get("characters") or []):
        a = get_asset(db, cid)
        if a is not None and a["kind"] == "character":
            main_png = _dta(data_dir, a["library_dir"]) / "main.png"
            if Path(main_png).exists():
                char_refs.append((a, main_png))
        if len(char_refs) >= 2:
            break
    # v1.2 场景参考：绑定场景资产有 main.png 即入第三槽（person+scene 官方组合）；
    # v1.4 场景名即使无图也可作文字锚（室内/室外跟描述，防白图带偏）
    scene_ref = None
    scene_name = ""
    for cid in (bound.get("scenes") or [])[:1]:
        a = get_asset(db, cid)
        if a is not None and a["kind"] == "scene":
            scene_name = a["name"]
            scene_png = _dta(data_dir, a["library_dir"]) / "main.png"
            if Path(scene_png).exists():
                scene_ref = (a, scene_png)
    extra_chars = ""
    scene_img_used = False
    krea_names = []
    second_anchor = ""
    if 1 <= len(char_refs) <= 2:
        from .paths import data_to_abs as _dta2
        blank = _dta2(data_dir, "_cache/blank_ref.png")
        if not Path(blank).exists():
            blank = _ensure_blank(data_dir)
        gray = _dta2(data_dir, "_cache/blank_gray.png")
        if not Path(gray).exists():
            gray = _ensure_blank(data_dir, "blank_gray.png", (128, 128, 128))
        # Krea2 快道优先（2026-09-13 用户实测 20-30s/页·风格原生贴合）：
        # 图1=场景 / 图2=人物（**槽序固定**——README 明示 scene 恒 image1、
        # person 恒 image2，交换即劣化）。双角色走**链式两段**（官方 workaround：
        # 先放 A → 结果作 image1 再插 B——拼接合成参考有「两人脸趋同」的
        # 官方确认缺陷，2026-09-13 README 实证后弃用拼接改链式）；缺失回落
        krea_tmpl = None
        try:
            krea_tmpl = resolve_template(db, "comic_page_krea2")
        except Exception:
            krea_tmpl = None
        if krea_tmpl is not None:
            tmpl = krea_tmpl
            krea_names = [c[0]["name"] for c in char_refs]
            # 用户定案（2026-09-13 终版）：分镜资产总数（角色主图+场景图）>2
            # 才特殊处理（stitch 拼接默认 / chain 链式可切，comic_dual_mode）；
            # ≤2 直接种槽——2角色+0场景时第二角色主图直进图1（提示词注明），
            # 1角色照常（图2 人物+图1 场景或灰）
            total_refs = len(char_refs) + (1 if scene_ref else 0)
            if total_refs > 2 and len(char_refs) == 2 \
                    and dual_mode == "stitch":
                char_path = _stitch_mains(data_dir,
                                          [char_refs[0][1], char_refs[1][1]])
                images = [
                    {"slot": "scene", "path": str(scene_ref[1]) if scene_ref else str(gray)},
                    {"slot": "char", "path": char_path},
                ]
            elif len(char_refs) == 2 and not scene_ref:
                images = [  # 2角色+0场景：直种双槽（图1=第二人物参考）
                    {"slot": "scene", "path": str(char_refs[1][1])},
                    {"slot": "char", "path": str(char_refs[0][1])},
                ]
            else:
                images = [
                    {"slot": "scene", "path": str(scene_ref[1]) if scene_ref else str(gray)},
                    {"slot": "char", "path": str(char_refs[0][1])},
                ]
            scene_img_used = scene_ref is not None
            params["megapixels"] = mp                      # 百万像素直传
            # 步数固定 10（2026-09-13 用户实测 4 步=10 步=30s——瓶颈在接地/编码
            # 等固定开销，步数非耗时项；质量档映射对 krea 道无意义）
            params["steps"] = 10
        else:
            try:
                ref_tmpl = resolve_template(db, "comic_page_ref")
            except Exception as exc:
                emit_log(db, "comfy", "warn",
                         f"参考注入模板不可用，本页退纯文生图：{exc}",
                         project_id=proj["id"], job_id=job["id"])
                ref_tmpl = None
            if ref_tmpl is not None:
                tmpl = ref_tmpl
                # v1.1 多图槽：char1 必填；char2 双角色；scene 第三槽。v1.4：缺省槽
                # 中性灰占位（白图偏亮把「新场景」带偏户外——真机判例：室内出成室外）
                # v1.3：canvas=空白画布作 latent 底（不锁构图——旧版 char1 直作底图
                # 把半身构图与摄影风格一并锁死，2026-09-13 真机判例）
                images = [
                    {"slot": "char1", "path": str(char_refs[0][1])},
                    {"slot": "char2", "path": str(char_refs[1][1]) if len(char_refs) == 2 else str(gray)},
                    {"slot": "scene", "path": str(scene_ref[1]) if scene_ref else str(gray)},
                    {"slot": "canvas", "path": str(blank)},
                ]
                scene_img_used = scene_ref is not None
                params["denoise"] = float(
                    (get_setting(db, "comfy") or {}).get("page_ref_denoise", 1.0))
                if len(char_refs) == 2:
                    extra_chars = char_refs[1][0]["name"]  # 双角色真参考——文字锚只需名字
    prompt = build_comic_prompt(db, proj, shot,
                                scene_mode=bool(images), extra_chars=extra_chars,
                                scene_img=scene_img_used, scene_name=scene_name,
                                krea_names=krea_names, second_anchor=second_anchor,
                                prev_summary=_prev_page_summary(db, shot),
                                continuity=(shot["continuity"]
                                            if "continuity" in shot.keys() else ""))
    comfy_cfg = get_setting(db, "comfy") or {}
    # v1.4：Lightning 默认关（4 步蒸馏画面糊·真机判例）——质量档步数 + cfg 3.5
    # （v4 锐利基线）；设 1.0 走 4 步草稿加速档（cfg 建议 2.5）
    lightning = float(comfy_cfg.get("page_ref_lightning", 0.0))
    if tmpl.id == "zimage_page_ref" and lightning > 0:
        params["lightning_strength"] = lightning
        params["steps"] = 4                      # Lightning 蒸馏 4 步（质量档步数不适用）
        params["cfg"] = float(comfy_cfg.get("page_ref_cfg", 2.5))
    elif tmpl.id == "zimage_page_ref":
        params["lightning_strength"] = 0.0
        params["cfg"] = float(comfy_cfg.get("page_ref_cfg", 3.5))
    # 落盘 pages/page_NNN.png（与动态漫 pages/ 目录约定同名不同项目空间）
    pages_dir = Path(data_dir) / "projects" / proj["slug"] / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    dest = pages_dir / f"page_{shot['seq']:03d}.png"

    def _submit_pass(prompt_text, pass_images, out_path, seed=None, tag=""):
        """一次 ComfyUI 提交→等图→落盘（链式两段的共用段）。"""
        _params = dict(params)
        if seed is not None:
            _params["seed"] = seed
        _wf, _ups = fill_workflow(
            tmpl, prompt=prompt_text,
            params=_params,
            images=pass_images,
            output_ctx={"project": proj["slug"], "asset": f"page-{shot['seq']}{tag}"},
            model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
        for up in _ups:
            comfy.upload_image(Path(up["path"]), up["name"])
        emit_log(db, "comfy", "info",
                 f"漫画页 {shot['seq']}{tag} 提交（模板 {tmpl.id}，{w}×{h}，"
                 f"{_params.get('steps', steps)}步"
                 + ("，角色参考注入" if pass_images else "") + "）",
                 project_id=proj["id"], job_id=job["id"])
        results = comfy.wait_and_collect(
            comfy.submit(_wf, client_id=f"cs-comic-{shot['id']}"), stall_seconds=600)
        img = next((r for r in results if r.get("_kind") == "image"), None)
        if img is None:
            raise RuntimeError(f"漫画页 {shot['seq']}{tag} 未返回图片")
        comfy.download(img["filename"], img.get("subfolder", ""), img.get("type", "output"),
                       out_path)
        return _wf

    if tmpl.id == "comic_page_krea2" and dual_mode == "chain" \
            and len(char_refs) == 2 and (len(char_refs) + (1 if scene_ref else 0)) > 2:
        # 链式两段（comic_dual_mode=chain，2026-09-13 用户决策保留可切）：
        # P1 场景+A → 中间产物作 P2 的图1 再插 B（tag 严禁含 /——上传名即路径）
        mid = pages_dir / f".chain_{shot['seq']:03d}.png"
        p1 = build_comic_prompt(db, proj, shot, scene_mode=True, extra_chars="",
                                scene_img=scene_img_used, scene_name=scene_name,
                                krea_names=[krea_names[0]] if krea_names else None)
        _submit_pass(p1, images, mid, tag="-链1")
        p2_images = [{"slot": "scene", "path": str(mid)},
                     {"slot": "char", "path": str(char_refs[1][1])}]
        import re as _re2
        p2_detail = _re2.sub(r"[（(]id=\d+[）)]", "",
                             (shot["description"] or "").strip())
        p2 = (f"在图1 的画面基础上加入图2 参考中的人物（{krea_names[1]}）："
              f"{p2_detail[:180]}。图1 中已有的人物与场景保持不变，"
              f"{krea_names[1]} 的外貌与图2 保持一致，按描述安排其位置与动作，"
              "画面高清锐利、细节清晰")
        st = ((proj["style_vis"] or proj["style"]) or "").strip()
        if st:
            p2 += f"。画风（严格执行）：{st}"
        if (proj["dialogue_mode"] or "bubble") == "bubble":
            p2 += "。画面上方适当留白，不要画任何文字、对话气泡、字幕"
        wf = _submit_pass(p2, p2_images, dest,
                          seed=random.randint(0, 2**31 - 1), tag="-链2")
        prompt = p2
        mid.unlink(missing_ok=True)
    else:
        wf = _submit_pass(prompt, images, dest)
    if job is not None:
        attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)

    # 干净副本（2026-09-13 用户建议）：后处理前存 page_NNN_clean.png——
    # 对白重排（改模式/气泡样式秒级本地重排不重出）+ 未来动态漫首尾帧源
    clean = pages_dir / f"page_{shot['seq']:03d}_clean.png"
    try:
        clean.write_bytes(dest.read_bytes())
    except OSError:
        pass  # 副本失败不拦主流程
    # 对白后处理（dialogue_mode: bubble/footer/none）
    dialogue = _ledger(shot).get("dialogue") or []
    mode = proj["dialogue_mode"] or "bubble"
    style = _bubble_style((proj["bubble_style"] if "bubble_style" in proj.keys() else "") or "")
    if mode != "none" and not _postprocess_dialogue(dest, dialogue, mode, style=style) and dialogue:
        emit_log(db, "comfy", "warn",
                 f"漫画页 {shot['seq']} 对白后处理未生效（缺 Pillow 或图片异常），页面无字幕条",
                 project_id=proj["id"], job_id=job["id"])

    update_shot(db, shot["id"], {"status": "comic_ready"})
    emit_log(db, "comfy", "info", f"漫画页 {shot['seq']} 已生成落盘",
             project_id=proj["id"], job_id=job["id"])
    # 终态推进不依赖 autopilot（2026-09-13 真机判例：手动批量重出全部页齐但
    # stage 停 storyboard_ready——「转视频/导出完成」语义按 comic_ready 门控，
    # 用户看不到入口）。最后一页落盘即 comic_ready + 日志收工
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") == "comic_output" \
            and proj["stage"] == "storyboard_ready":
        from .shots import list_shots
        missing = [s["id"] for s in list_shots(db, shot["project_id"])
                   if not s["disabled"]
                   and not (pages_dir / f"page_{s['seq']:03d}.png").exists()]
        if not missing:
            from .projects import set_stage
            set_stage(db, shot["project_id"], "comic_ready")
            emit_log(db, "autopilot", "info", "全部漫画页就绪——漫画完成（可导出 PDF/长图，或「🎬 转视频」）",
                     project_id=proj["id"], job_id=job["id"])
    return dest


def _postprocess_dialogue(page_png, dialogue, mode, style: dict | None = None) -> bool:
    """对白后处理：bubble=顶部交错气泡 / footer=底部字幕条（Pillow 可选依赖）。

    none=不呈现。返回 False 表示需要处理但未成功（Pillow 缺失/图片
    异常）——调用方负责 warn 透明。
    """
    if not dialogue or mode == "none":
        return True  # 无需处理 ≠ 失败
    if mode == "bubble":
        return _draw_bubbles(page_png, dialogue, style or {})
    if mode != "footer":
        return True
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    try:
        img = Image.open(page_png).convert("RGBA")
    except Exception:
        return False
    font = _footer_font()
    max_w = img.width - 40
    rows: list[str] = []
    for d in dialogue:
        if not d.get("line"):
            continue
        rows.extend(_wrap_footer(f"{d.get('speaker', '')}：{d['line']}", font, max_w))
    rows = rows[:8]  # 防爆：极端长对白最多 8 行（约 1/4 页高）
    line_h = 32
    bar_h = len(rows) * line_h + 16
    # 半透明黑底条：白字在浅色漫画页上不可见（真机观感），压暗后清晰
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [0, img.height - bar_h - 10, img.width, img.height], fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    y = img.height - bar_h + 8
    for t in rows:
        draw.text((20, y), t, fill="white", font=font)
        y += line_h
    img.save(page_png)
    return True


_FOOTER_FONT_CANDIDATES = (
    "msyh.ttc",  # Windows 微软雅黑
    "C:/Windows/Fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/msyh.ttc",  # WSL 挂 Windows 盘（与生产同字形）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # Debian/WSL 文泉驿正黑
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Debian/WSL
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",  # Arch 系
    "/System/Library/Fonts/PingFang.ttc",  # macOS 苹方
)


def _footer_font(size: int = 28):
    """footer 字体：跨平台 CJK 回退链——WSL/Linux 无 msyh.ttc 时原实现落
    load_default() 点阵小字（真机观感差），补 Noto/苹方常见路径兜底。"""
    from PIL import ImageFont
    for path in _FOOTER_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow<10.1 的 load_default 无 size 参数
        return ImageFont.load_default()


def _wrap_footer(text: str, font, max_width: int) -> list[str]:
    """按像素宽逐字换行（CJK 无空格断词）：替代原 t[:80] 硬截断——
    长台词以前直接丢字，字幕条只显示前 80 字。"""
    lines, cur = [], ""
    for ch in text:
        if cur and font.getlength(cur + ch) > max_width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


# ---- 气泡渲染（2026-09-13 二期①）：Pillow 顶部交错真气泡 ----
# 设计定稿：宽度随内容自适应（上限 40% 页宽）；透明度只作用气泡底色
# （100%=背景全透明只剩描边+文字，字永远清晰）；字号 0=随页宽自适应。

_BUBBLE_DEFAULT = {"opacity": 85, "font_color": "#222222", "font_size": 0}


def _bubble_style(raw) -> dict:
    """解析 projects.bubble_style JSON（容错：空/坏 JSON/缺键→默认；opacity 钳 0~100）。"""
    try:
        d = json.loads(raw) if raw else {}
        if not isinstance(d, dict):
            raise ValueError
    except (ValueError, TypeError):
        d = {}
    out = dict(_BUBBLE_DEFAULT)
    if isinstance(d.get("opacity"), (int, float)):
        out["opacity"] = min(100, max(0, int(d["opacity"])))
    if isinstance(d.get("font_color"), str) and d["font_color"]:
        out["font_color"] = d["font_color"]
    if isinstance(d.get("font_size"), (int, float)) and int(d["font_size"]) > 0:
        out["font_size"] = int(d["font_size"])
    return out


def _hex_rgba(hex_color: str) -> tuple:
    """'#rrggbb' → (r,g,b,255)；非法回落默认深灰。"""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        except ValueError:
            pass
    return (34, 34, 34, 255)


_BUBBLE_PAD_X, _BUBBLE_PAD_Y = 14, 10


# 气泡候选分区（x0,y0,x1,y1 比例）——智能定位在分区里挑最空的落（2026-09-13
# 用户需求：避人物/重要部位，看站位选空闲区——左上/中部常空）
_BUBBLE_ZONES = (
    (0.04, 0.03, 0.46, 0.30),   # 左上
    (0.54, 0.03, 0.96, 0.30),   # 右上
    (0.04, 0.33, 0.46, 0.62),   # 左中
    (0.54, 0.33, 0.96, 0.62),   # 右中
    (0.04, 0.65, 0.46, 0.92),   # 左下
    (0.54, 0.65, 0.96, 0.92),   # 右下
)


def _zone_energies(img):
    """各分区边缘能量（人物/细节=高频）升序——Pillow FIND_EDGES 缩到 64x96
    求和，纯函数可测、毫秒级。img=None → None（回落旧布局）。"""
    try:
        from PIL import ImageFilter
        small = img.convert("L").resize((64, 96))
        edges = small.filter(ImageFilter.FIND_EDGES)
        px = edges.load()
        out = []
        for zx0, zy0, zx1, zy1 in _BUBBLE_ZONES:
            x0, y0 = int(zx0 * 64), int(zy0 * 96)
            x1, y1 = int(zx1 * 64), int(zy1 * 96)
            tot = sum(px[x, y] for y in range(y0, y1) for x in range(x0, x1))
            out.append(tot / max(1, (x1 - x0) * (y1 - y0)))
        return out
    except Exception:
        return None


def _bubble_layout(page_w, page_h, dialogue, font, max_ratio=0.4, page_img=None):
    """纯布局：每句一气泡，宽度随内容自适应（上限 max_ratio*页宽）。

    智能定位（2026-09-13 用户需求：气泡别盖人物/重要部位——低透明度时
    挡脸）：page_img 在场时按六分区边缘能量升序落位（最空的分区先放，
    分区用尽再按能量序叠加）；无图回落左右交替顶部流（旧行为）。
    返回 (boxes, 截断数)：boxes = [(x, y, w, h, lines)]。
    最多 4 气泡防爆（同 footer 8 行思路）。"""
    size = getattr(font, "size", None) or 28
    line_h = int(size * 1.45)
    max_w = int(page_w * max_ratio)
    items = []
    for d in dialogue[:4]:
        line = f"{d.get('speaker', '')}：{d.get('line', '')}".lstrip("：").strip()
        if not line:
            continue
        lines = _wrap_footer(line, font, max_w - 2 * _BUBBLE_PAD_X)
        w = int(min(max(font.getlength(t) for t in lines) + 2 * _BUBBLE_PAD_X, max_w))
        h = len(lines) * line_h + 2 * _BUBBLE_PAD_Y
        items.append((w, h, lines))
    dropped = max(0, len(dialogue) - 4)

    energies = _zone_energies(page_img) if page_img is not None else None
    if energies is not None:
        order = sorted(range(len(_BUBBLE_ZONES)), key=lambda i: energies[i])
        zone_y = {}   # 分区内 y 游标（同分区多气泡纵向堆叠）
        boxes = []
        for i, (w, h, lines) in enumerate(items):
            zi = order[i % len(order)]
            zx0, zy0, zx1, zy1 = _BUBBLE_ZONES[zi]
            zx0, zy0 = int(zx0 * page_w), int(zy0 * page_h)
            zx1, zy1 = int(zx1 * page_w), int(zy1 * page_h)
            x = min(zx0, max(zx0, zx1 - w))
            y = zone_y.get(zi, zy0)
            if y + h > zy1:      # 分区放不下→回顶部（防爆越界）
                y = zy0
            boxes.append((x, y, w, h, lines))
            zone_y[zi] = y + h + int(page_h * 0.015) + 14
        return boxes, dropped

    # 无图回落：左右双列顶部流（同列间距含尾巴，2026-09-13 样张视检修正）
    y_top = int(page_h * 0.04)
    gap = int(page_h * 0.015)
    col_y = [y_top, y_top]
    boxes = []
    for i, (w, h, lines) in enumerate(items):
        col = i % 2
        x = int(page_w * 0.06) if col == 0 else int(page_w * 0.94) - w
        y = col_y[col]
        boxes.append((x, y, w, h, lines))
        col_y[col] = y + h + gap + 14
    return boxes, dropped


def _draw_bubbles(page_png, dialogue, style) -> bool:
    """画气泡：白底圆角矩形+描边+底边小三角尾（透明度只作用底色），
    文字颜色/字号按 style。返回 False=Pillow 缺失/图片异常。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    try:
        img = Image.open(page_png).convert("RGBA")
    except Exception:
        return False
    page_w, page_h = img.size
    st = _bubble_style(style if isinstance(style, (str, dict)) and style else None) \
        if not isinstance(style, dict) else {**_BUBBLE_DEFAULT, **(style or {})}
    fsize = st.get("font_size") or max(16, min(34, page_w // 36))
    font = _footer_font(fsize)
    line_h = int((getattr(font, "size", None) or fsize) * 1.45)
    boxes, dropped = _bubble_layout(page_w, page_h, dialogue, font, page_img=img)
    bg_alpha = int(255 * (100 - st.get("opacity", 85)) / 100)  # 100% → 0（全透明）
    fill = (255, 255, 255, bg_alpha)
    edge = (35, 35, 35, 255)  # 描边不透明近黑（漫画风粗边）——透明底时描边+文字构成轮廓气泡
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x, y, w, h, _lines in boxes:
        od.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=fill,
                             outline=edge, width=4)
        cx = x + w // 2  # 底边中央小尾巴指向画面主体
        od.polygon([(cx - 9, y + h - 2), (cx + 9, y + h - 2), (cx, y + h + 14)],
                   fill=fill, outline=edge)
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    color = _hex_rgba(st.get("font_color"))
    for x, y, w, h, lines in boxes:
        ty = y + _BUBBLE_PAD_Y
        for t in lines:
            draw.text((x + _BUBBLE_PAD_X, ty), t, fill=color, font=font)
            ty += line_h
    img.convert("RGB").save(page_png)
    return True
