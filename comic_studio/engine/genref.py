# comic_studio/engine/genref.py
"""gen_ref 处理器：为资产生成参考图并落库 views/（spec 门1 前置）。"""
import json
import random
import re

from .assets import get_asset
from .logbus import emit as emit_log
from .paths import data_to_abs
from .queue.worker import register
from .settings import get_setting
from .workflows.filler import fill_workflow
from .workflows.registry import resolve_template

KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具"}
KIND_SUFFIX = {
    "character": "，角色三视图设定图：同一画面中从左到右依次为 正面全身、左侧全身、背面全身，"
                 "三个视角必须明显不同且各占三分之一，全身像，白色干净背景",
    # 2026-08-28 真机：道具"眼镜"总带人物模特、场景"保健室"总有人——弱表述模型
    # 不当回事，按 Z-Image 正向纠错惯例改为强禁令（suffix + 尾缀双重强调）
    "scene": "，空场景概念设定图（人物未入画），环境全景，画面中禁止出现任何人物、人形剪影",
    "prop": "，产品静物摄影图：道具单体居中特写，纯白背景，画面中禁止出现任何人物、人手、人形剪影",
}

# ZImage-Turbo 规范（data/ZImage-Turbo 完整版本地技能模板.md，2026-08-25 接入）：
# 负向词完全无效——纠错全部正向写入；中英混编（中文意境+英文质感）；精简适配 8 步推理
ZIMAGE_TAIL = {
    "character": "，cinematic color grading，sharp focus，ultra-detailed，8k，"
                 "避免畸形肢体，避免多余手指，避免五官扭曲，无蜡像塑料感，无文字水印，画面完整",
    "scene": "，cinematic color grading，sharp focus，ultra-detailed，8k，"
             "无文字水印，画面完整不裁切，画面中无人物",
    "prop": "，sharp focus，ultra-detailed，8k，材质纹理真实清晰，"
            "无文字水印，画面干净完整，无人物无手部",
}

# 写实/真人意图检测（2026-08-27 真机：自定义"真人电影"仍出二次元——"立绘"措辞+
# Turbo cfg=1 文本话语权弱+模型先验偏插画，三方叠加。检测到写实意图即换摄影向措辞+增强词）
PHOTO_RE = re.compile(r"写实|真人|实拍|摄影|photoreal|realistic|人像|电影质感")
PHOTO_BOOST = "真人实拍质感，真实皮肤纹理与毛孔细节，自然光影，35mm 镜头景深，照片级真实"


def is_photo_style(style_text: str) -> bool:
    return bool(PHOTO_RE.search(style_text or ""))


_APPEARANCE_LINE = re.compile(r"^([一-龥]{1,5})[：:]\s*(.+)$")


def parse_appearance_fields(detail: str) -> list[tuple[str, str]]:
    """行模板 → 有序 [(label, value)]，「无」值行与空行已滤；非行模板行 label=""。"""
    out = []
    for line in (detail or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _APPEARANCE_LINE.match(line)
        label, value = (m.group(1), m.group(2).strip()) if m else ("", line)
        if not value or value == "无":
            continue
        out.append((label, value))
    return out


def condense_appearance(detail: str) -> str:
    """外貌行模板 → 紧凑自然语言（2026-08-27 真机：majicmix 下男性角色变女性）。
    「无」值行对 CLIP 是噪声，丢弃；性别转强词并加英文锚（CLIP 对 male/man
    token 敏感，majicmix 女性先验强，必须显式压）；年龄/发型/服装并成短句。
    非行模板的自由文本原样返回。"""
    fields_list = parse_appearance_fields(detail)
    if not fields_list:
        return (detail or "").strip()
    fields = {}
    for label, value in fields_list:
        if label and label not in fields:
            fields[label] = value
    if not any(label for label, _ in fields_list):  # 纯自由文本
        return "，".join(v for _, v in fields_list)
    gender = fields.pop("性别", "")
    age = fields.pop("年龄", "")
    hair = fields.pop("发色发型", "")
    clothes = fields.pop("服装", "")
    if gender in ("男", "男性"):
        gender_cn, gender_en = "男性", "man"
    elif gender in ("女", "女性"):
        gender_cn, gender_en = "女性", "woman"
    else:
        gender_cn, gender_en = gender, ""
    s = (age + ("岁" if age and not age.endswith("岁") else "") ) + hair + gender_cn
    if gender_en:
        s += f"（{gender_en}）"
    if clothes:
        s += "，穿" + clothes
    for label, value in fields_list:
        if label in ("", "性别", "年龄", "发色发型", "服装"):
            continue
        if fields.get(label) == value:
            # 保留「标签：值」（2026-09-13 真机判例：体型调高挑后主图仍偏胖——
            # 裸值夹在肤色后是弱约束噪声；中文流（Z-Image qwen 编码器）标签即
            # 语义锚。性别/年龄/发型/服装仍并自然短句不动）
            s += f"，{label}：{value}"
    return s.strip("，")


# ===== 提示词方言：tags_en（SD 系 CLIP 读不懂中文——中文提示词对它是噪声 token，
# 2026-08-27 真机：t2i_ref 中文提示词 → 输出连人都不是）=====
_EN_HAIR_COLORS = (("黑", "black"), ("白", "white"), ("金", "blonde"), ("红", "red"),
                   ("棕", "brown"), ("褐", "brown"), ("蓝", "blue"), ("银", "silver"),
                   ("灰", "gray"), ("粉", "pink"), ("紫", "purple"), ("绿", "green"))
_EN_HAIR_STYLES = (("短发", "short hair"), ("长发", "long hair"), ("马尾", "ponytail"),
                   ("双马尾", "twintails"), ("卷发", "curly hair"), ("直发", "straight hair"),
                   ("寸头", "buzz cut"), ("刘海", "bangs"), ("辫", "braid"))
EN_QUALITY_TAIL = "cinematic color grading, sharp focus, ultra-detailed, 8k, best quality"
EN_PHOTO_BOOST = ("photorealistic, realistic skin texture and pores, natural lighting, "
                  "35mm depth of field")


def _hair_en(text: str) -> str:
    color = next((en for zh, en in _EN_HAIR_COLORS if zh in text), "")
    styles = [en for zh, en in _EN_HAIR_STYLES if zh in text]
    if not color and not styles:
        return text  # 识别不出原样返回（弱引导好过没有）
    return " ".join(([color + " colored"] if color else []) + (styles or ["hair"]))


def build_gen_prompt_tags_en(asset_row, style: str = "", era: str = "",
                             variant: str = "views"):
    """英文标签流组装（prompt_style: tags_en 模板用）。结构化字段确定性映射；
    服装等自由中文文本无法离线翻译、弱通过；era 为中文后缀此处跳过。"""
    kind = asset_row["kind"]
    detail = json.loads(asset_row["appearance_json"]).get("detail", "")
    fields_list = parse_appearance_fields(detail)
    d: dict[str, str] = {}
    for label, value in fields_list:
        if label and label not in d:
            d[label] = value
    tags: list[str] = []
    if kind == "scene":
        tags.append("scenery, environment, no humans")
    elif kind == "prop":
        tags.append("single prop object, product shot")
    g = d.get("性别", "")
    if g in ("男", "男性"):
        tags += ["1boy", "solo"]
    elif g in ("女", "女性"):
        tags += ["1girl", "solo"]
    age = d.get("年龄", "")
    if age:
        tags.append(f"{age} years old" if age.isdigit() else age)
    if d.get("发色发型"):
        tags.append(_hair_en(d["发色发型"]))
    if d.get("服装"):
        tags.append(d["服装"])  # 中文弱通过——离线无翻译，占位保结构
    for label, value in fields_list:
        if label in ("", "性别", "年龄", "发色发型", "服装"):
            continue
        if d.get(label) == value:
            tags.append(value)
    if kind == "character" and variant == "main":
        tags += ["full body", "standing", "neutral expression", "simple white background"]
    elif kind == "character":
        tags.append("character sheet")
    if is_photo_style(style):
        tags.append(EN_PHOTO_BOOST)
    elif style.strip():
        tags.append(style.strip().rstrip("。；;，,"))
    tags.append(EN_QUALITY_TAIL)
    ctx = {"project": f"p{asset_row['source_project']}", "asset": str(asset_row["id"])}
    return ", ".join(t for t in tags if t), ctx


def build_gen_prompt(asset_row, style: str = "", era: str = "",
                     variant: str = "views"):
    """style：项目级画风；era：时代背景；variant：views=三视图设定图（单段回退），
    main=角色主图（两段式第一段，无三视图约束）。"""
    detail = condense_appearance(json.loads(asset_row["appearance_json"]).get("detail", ""))
    base = KIND_LABEL[asset_row["kind"]] + "：" + asset_row["name"]
    if detail:
        base += "。" + detail.strip().rstrip("。；;，,")
    kind = asset_row["kind"]
    if variant == "main" and kind == "character":
        # "立绘"是二次元词汇（真机教训）；写实意图时用摄影向"全身照"措辞
        suffix = ("，角色主图：单人物全身照，站姿自然，表情中性，白色干净背景"
                  if is_photo_style(style) else
                  "，角色主图：单人物全身像，站姿自然，表情中性，白色干净背景")
    else:
        suffix = KIND_SUFFIX.get(kind, "")
    prompt = base + suffix
    style = style.strip().rstrip("。；;，,").strip()
    if style:
        prompt += "。" + style   # 风格段：主导整体画风
    if is_photo_style(style):
        prompt += "。" + PHOTO_BOOST  # 写实增强（cfg=1 下弱文本需强词）
    era = (era or "").strip()
    if era:
        from .era import era_suffix
        prompt += "。" + era_suffix(era)
    prompt += ZIMAGE_TAIL.get(kind, "")  # Turbo 质量与正向纠错尾缀
    if kind == "character" and variant != "main":
        prompt += "。严格三视图布局：正面、左侧、背面各一个，禁止视角重复"  # 结构收尾再强调
    ctx = {"project": f"p{asset_row['source_project']}", "asset": str(asset_row["id"])}
    return prompt, ctx


def _fill_missing_slots(data_dir, tmpl, images):
    """多图槽模板的缺槽补中性灰占位（2026-09-14 真机 400：comic_page_krea2
    当主图模板，未提供的 LoadImage 槽留着 char1.png 默认引用——ComfyUI 校验
    「文件不存在」直接 400。同 ref2va 音频槽静音占位判例）。
    返回补全后的 images 副本（无缺槽原样返回）。"""
    provided = {im["slot"] for im in (images or [])}
    missing = [spec for spec in tmpl.inject_images if spec["slot"] not in provided]
    if not missing:
        return images
    from .comicgen import _ensure_blank
    gray = _ensure_blank(data_dir, "blank_gray.png", (128, 128, 128))
    return list(images or []) + [{"slot": spec["slot"], "path": gray}
                                 for spec in missing]


def _t2i_to_file(db, data_dir, comfy, tmpl, prompt, dest, ctx, job, label,
                 images=None, krea_style: str = ""):
    """单段 t2i：组工作流 → 提交 → 等待 → 下载到 dest（主图与回退路径共用）。
    images：模板声明图片槽时传入（如文+图重绘的 ref 槽）；未提供的槽自动
    补灰占位防 ComfyUI 400。
    krea_style：projects.krea2_style（"lib|style"）——Krea2 工作台风格槽注入
    （2026-09-20 用户实测：提示词文字段推不动部分 Krea2 模型，工作台槽才是
    强杠杆）；模板未声明该参数则自动忽略（zimage 道零影响）。"""
    if comfy is None:
        raise RuntimeError("gen_ref 需要 ComfyUI 端点（settings.comfy.base_url）")
    images = _fill_missing_slots(data_dir, tmpl, images)
    # 模板级步数（2026-09-14 template_params，设置页模型切换区按模板配）：
    # 0/缺省=模板内置——t2i 步数对耗时影响大（Krea2 文生图等）
    params = {"seed": random.randint(0, 2**31 - 1)}
    if krea_style:
        from .stylepresets import parse_krea2_style, resolve_style_lib
        _lib, _name = parse_krea2_style(krea_style)
        if _lib:
            # 槽值=原始 stem（ComfyUI 枚举带 krea2_ 前缀；存库值是剥前缀显示名
            # ——2026-09-23 猫物语真机：直注显示名 value_not_in_list 400）
            params["krea_style_lib"] = resolve_style_lib(_lib)
            params["krea_style"] = _name
    _steps = int(((get_setting(db, "template_params") or {}).get(tmpl.id) or {})
                 .get("steps") or 0)
    if _steps > 0:
        params["steps"] = _steps
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params=params,
        images=images, output_ctx=ctx,
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
    for up in uploads:
        comfy.upload_image(up["path"], up["name"])
    from .jobs import attach_snapshot
    attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)
    emit_log(db, "comfy", "info", f"{label}提交（模板 {tmpl.id}）",
             project_id=job["project_id"], job_id=job["id"])
    images = comfy.wait_and_collect(
        comfy.submit(wf, client_id=f"cs-job-{job['id']}"), stall_seconds=600,
        on_interrupt=lambda: emit_log(db, "comfy", "warn",
                                      f"job {job['id']} 失速，已 interrupt",
                                      project_id=job["project_id"], job_id=job["id"]))
    if not images:
        raise RuntimeError(f"{label}：ComfyUI 未返回图片")
    dest.parent.mkdir(parents=True, exist_ok=True)
    comfy.download(images[0]["filename"], images[0].get("subfolder", ""),
                   images[0].get("type", "output"), dest)
    emit_log(db, "comfy", "info", f"{label}已生成并落盘",
             project_id=job["project_id"], job_id=job["id"])
    return dest


@register("gen_ref")
def handle_gen_ref(db, data_dir, job, comfy):
    payload = json.loads(job["payload_json"] or "{}")
    asset = get_asset(db, payload["asset_id"])
    if asset is None:
        raise ValueError(f"资产不存在: {payload['asset_id']}")
    from .projects import get_project
    proj = get_project(db, asset["source_project"])
    # 画风拆层（方案A 2026-08-27）：图像生成优先视觉子集 style_vis，
    # 叙事/剪辑词留在 style 给视频提示词——"场景切换流畅""剪辑节奏"对 T2I 是噪声
    style = (proj["style_vis"] or proj["style"]) if proj else ""
    era = proj["era"] if proj is not None and "era" in proj.keys() else ""
    krea2_style = ((proj["krea2_style"] if "krea2_style" in proj.keys() else "")
                   if proj is not None else "")
    cv_tmpl = None
    if asset["kind"] == "character":
        from .workflows.registry import ManifestError
        try:
            cv_tmpl = resolve_template(db, "character_views")
        except ManifestError:
            cv_tmpl = None  # 未映射/模板缺失 → 单段回退
    views_dir = data_to_abs(data_dir, asset["library_dir"]) / "views"
    dest = views_dir / "sheet.png"
    stage = payload.get("stage") or "all"
    if stage not in ("all", "main", "views"):
        raise ValueError(f"未知 stage: {stage}")
    if cv_tmpl is not None:
        # 两段式（2026-08-25 需求）：zimage 主图（可重复生成）→ Krea2 四视图派生
        # stage 粒度：all=两段；main=仅主图（sheet 不动、不标 stale）；
        # views=仅从现有主图重派生三视图（缺主图时先自动补）
        main_png = data_to_abs(data_dir, asset["library_dir"]) / "main.png"
        ctx = {"project": f"p{asset['source_project']}", "asset": str(asset["id"])}
        if stage in ("all", "main") or not main_png.exists():
            # 提示词方言（2026-08-27）：按主图模板声明分发——SD 系 CLIP 用英文标签流
            main_builder = (build_gen_prompt_tags_en
                            if getattr(resolve_template(db, "t2i"), "prompt_style", "") == "tags_en"
                            else build_gen_prompt)
            main_prompt, _ = main_builder(asset, style=style, era=era, variant="main")
            # 主图模板若声明图片槽（文+图重绘类，如 xf_zimage_ti2i）：
            # 有主图 → 作 ref 传入重绘；无主图/🎨重设计 → 纯文生图（zimage_t2i）。
            # redesign（2026-09-20 用户判例：换画风后重生主图每次都差不多——带图
            # 槽模板的旧 main.png 参考把构图与人脸锚死，画风词推不动；重设计强制
            # 走纯文生图让画风全权驱动）
            main_tmpl = resolve_template(db, "t2i")
            main_images = None
            redesign = bool(payload.get("redesign"))
            if main_tmpl.inject_images and main_png.exists() and not redesign:
                # 槽位偏好（2026-09-14）：旧主图是「人物」参考——多槽模板
                # 优先进 char/char1 槽（Krea2 工作台图2=人物、zimage_page_ref
                # char1=身份参考）；无人物语义槽才退第一槽（xf_zimage_ti2i）
                _slots = [im["slot"] for im in main_tmpl.inject_images]
                _pref = next((s for s in ("char", "char1") if s in _slots),
                             _slots[0])
                main_images = [{"slot": _pref, "path": str(main_png)}]
            elif main_tmpl.inject_images:
                from .workflows import registry as _reg
                boot = _reg.scan_templates(_reg.TEMPLATE_ROOT).get("zimage_t2i")
                if boot is None or boot.inject_images:
                    raise ValueError(
                        f"主图模板 {main_tmpl.id} 需要图片输入，且无现有主图可传"
                        f"（可先把 t2i 映射切回纯文生图模板生成首张主图）")
                main_tmpl = boot
                emit_log(db, "comfy", "info",
                         ("🎨 重设计：忽略旧主图参考" if redesign else "无现有主图")
                         + f"，用纯文生图 {boot.id} 生成主图",
                         project_id=job["project_id"], job_id=job["id"])
            # 库风格激活的角色主图：风格 prompt 自带场景词汇会稀释「单人物」
            # 锚定（2026-09-20 用户实测：选风格后背景常冒出多个人物）——
            # 正向强化禁令（ZImage/Krea2 负向词通道不可靠）
            if krea2_style and asset["kind"] == "character":
                main_prompt += ("。画面中有且仅有一个人物，背景不得出现任何"
                                "其他人物、人形剪影或额外角色")
            if getattr(main_tmpl, "prompt_expand", False):
                main_prompt = expand_image_prompt(db, main_prompt,
                                                  mode=main_tmpl.prompt_expand)
            _t2i_to_file(db, data_dir, comfy, main_tmpl, main_prompt, main_png, ctx,
                         job, label=f"资产「{asset['name']}」主图", images=main_images,
                         krea_style=krea2_style)
        if stage in ("all", "views"):
            # 提示词不注入：四视图走工作流内置触发词（用户勘误 2026-08-25——
            # 参数只有主图 body 槽 + 随机 seed，步数等保持工作流默认）
            wf, uploads = fill_workflow(
                cv_tmpl, prompt=None,
                params={"seed": payload.get("seed") or random.randint(0, 2**31 - 1)},
                images=[{"slot": "body", "path": str(main_png)}], output_ctx=ctx,
                model_overrides=(get_setting(db, "model_overrides") or {}).get(cv_tmpl.id))
            for up in uploads:
                comfy.upload_image(up["path"], up["name"])
            from .jobs import attach_snapshot
            attach_snapshot(db, job["id"], prompt="(四视图：工作流内置触发词，主图作种子)",
                            workflow=wf, template_id=cv_tmpl.id)
            emit_log(db, "comfy", "info",
                     f"资产「{asset['name']}」参考图提交（模板 {cv_tmpl.id}，主图派生三视图）",
                     project_id=job["project_id"], job_id=job["id"])
            images = comfy.wait_and_collect(
                comfy.submit(wf, client_id=f"cs-job-{job['id']}"), stall_seconds=600,
                on_interrupt=lambda: emit_log(db, "comfy", "warn",
                                              f"job {job['id']} 失速，已 interrupt",
                                              project_id=job["project_id"], job_id=job["id"]))
            if not images:
                raise RuntimeError("ComfyUI 未返回任何输出图片")
            dest.parent.mkdir(parents=True, exist_ok=True)
            comfy.download(images[0]["filename"], images[0].get("subfolder", ""),
                           images[0].get("type", "output"), dest)
            emit_log(db, "comfy", "info", f"资产「{asset['name']}」参考图已生成并落盘",
                     project_id=job["project_id"], job_id=job["id"],
                     data={"path": f"{asset['library_dir']}/views/sheet.png"})
    else:
        # 单段（场景/道具，或 character_views 未映射的角色回退；stage 仅 all 有意义）
        _tmpl = resolve_template(db, "t2i")
        _builder = (build_gen_prompt_tags_en
                    if getattr(_tmpl, "prompt_style", "") == "tags_en" else build_gen_prompt)
        prompt, ctx = _builder(asset, style=style, era=era)
        _t2i_to_file(db, data_dir, comfy, _tmpl, prompt, dest, ctx, job,
                     label=f"资产「{asset['name']}」参考图", krea_style=krea2_style)
    if stage == "main":
        return  # 仅换主图：sheet 未变，无需 stale 联动
    from .shots import mark_stale_for_asset
    n = mark_stale_for_asset(db, asset["id"])
    if n:
        emit_log(db, "storyboard", "warn",
                 f"资产「{asset['name']}」参考图已更新：{n} 个引用它的分镜标记为 stale",
                 project_id=job["project_id"], job_id=job["id"])


# ── 官方 Qwen-Image-2.1 扩写规范（2026-10-01 接入）──────────────────────────
# 素材源 E:/AI/project/qwen-image-2.1-skill（阿里官方提示词重写系统提示词的
# 技能化包装，Apache 2.0；官方规范版权归阿里）。三套按模式路由：
# t2i-zh / t2i-en（8 步观察者流，语言随 comfy.prompt_expand_lang 旋钮——
# 用户定调两版真机 A/B、效果一致优先中文）/ edit（属性解耦流，官方规则
# 正文随用户语言=恒中文）。共同硬约束：锚定逐字保留、禁空词、单段无换行。

EXPAND_T2I_ZH = """你是 Qwen-Image-2.1 文生图提示词重写器。把用户的画面提示词扩写为一段细节丰富的中文描述——你是在**描述一张已经完成的作品**（第三人称、现在时、陈述句），不是在下指令，也不是在跟谁对话。

结构（按序执行，后步不改前步）：
1. 开场锚定句：一句话点明媒介与画风（照片/插画/海报/特写…）、主体、背景与色调倾向
2. 按空间走画面：用 8~14 个方位词依次描述（左上角/顶部横带/右侧/下三分之一/中央…），方位词必须触达四角、边缘与中央，不能只聚在中间；约三分之一的句子以方位词开头
3. 独立光影句：光源、方向、质感、投影与高光——必须显式写出，不许只留暗示
4. 收尾构图总结句：恰好一句，收拢平衡、色调、画风与情绪——之后不得再接第二句总结

词汇纪律：颜色带修饰（深藏青、暖赭、灰白）；材质具名（拉丝金属、粗麻、磨砂塑料）；逐项枚举不概括（不写「若干物品」「各种装饰」）；不确定的细节用「看似/可能」对冲，只在用户点定的内容上斩钉截铁。

硬约束：
- **必须逐字保留原提示词中的全部锚定信息**（人物身份/外貌描述、画风段、时代段、场景环境、构图指令、禁止事项如禁字与留白指令）——只能增补，不得改写、弱化或丢失
- 禁空洞修饰词：8K、4K、杰作、获奖、大师级神作等一律不写
- 画幅比例、分辨率、像素数绝不写进正文
- 长度约为原文的 2~3 倍，输出为单段连续文字（无换行）
- 只输出扩写后的提示词本身，不要任何解释或前缀"""

EXPAND_T2I_EN = """You are a Qwen-Image-2.1 text-to-image prompt rewriter. Expand the user's image prompt into one long, richly detailed English paragraph — you are **describing a finished image as an observer** (third person, present tense, declarative), never issuing commands and never addressing anyone.

Structure (work in order; later steps never revise earlier ones):
1. Opening anchor sentence: name the medium and style (photograph, illustration, poster, close-up...), the subject, and the background with its palette.
2. Walk the frame with 8-14 positional phrases (upper-left corner, across the top band, on the far right, lower third, dead centre...) reaching the corners, edges and centre — never clustered mid-frame; roughly a third of sentences open on the positional phrase itself.
3. A dedicated lighting sentence: source, direction, quality, the shadows and highlights it leaves — explicit, never implied.
4. Exactly one closing composition sentence on balance, palette, style and mood — no second summary after it.

Vocabulary discipline: colours carry modifiers (deep navy, warm terracotta, off-white); materials are named (brushed metal, coarse linen, matte plastic); enumerate, never summarise (never "several items"); hedge what you cannot be certain of ("appears to be", "likely") and be definite only about what the user fixed.

Hard constraints:
- Preserve EVERY constraint from the original prompt — character identity and appearance, style, era, scene, composition directives, prohibitions (e.g. no text, headroom for speech bubbles) — translating them into English and keeping them fully anchored; add, never rewrite, weaken or drop.
- No empty quality boosters: 8K, 4K, masterpiece, award-winning, hyper-detailed.
- Never state aspect ratios, resolutions or pixel counts in the text.
- Length about 2-3 times the original; one single continuous paragraph with no line breaks.
- Output only the rewritten prompt itself, no explanations or prefixes."""

EXPAND_EDIT_ZH = """你是 Qwen-Image-2.1 图像编辑提示词重写器（带参考图的编辑/参考条件化任务）。把用户的编辑提示词扩写为一段指令清晰的中文描述——以**操作开头**（例：将图1中人物的…替换为…），站在「手上只有输入图」的视角写，不是描述成品。

核心原则——强力属性解耦：
- 用户点名的属性：改到清晰可辨的程度，不许轻描淡写地「意思一下」
- 未点名的一切：用整段保留条款锁住（例：「图中其余人物、物品、光影与背景与原图完全一致」）——保留物**不要具体重描**，对保留内容的详细描述会被模型当成生成指令引起漂移
- 人物身份来自参考图时指向图片本身（「图1中人物」），不要用文字重述五官发型
- 不新增用户没要的操作，不擅自清理画面瑕疵；操作会露出新区域时，交代清楚露出区域保持画面物理连贯
- 画内文字：只在用户明确要求时出现，逐字双引号引用、单语不混排

硬约束：
- **必须逐字保留原提示词中的全部锚定信息**（参考图指向、身份保持条款、场景约束、画风段、禁止事项）——只能增补，不得改写或丢失
- 禁空洞修饰词（8K/杰作/获奖）；画幅比例与分辨率绝不进正文
- 长度约为原文的 2~3 倍，输出为单段连续文字（无换行）
- 只输出扩写后的提示词本身，不要任何解释或前缀"""


# 官方 validate_prompt.py 禁词表移植（英文边界用环视——中文相邻处 \b 不生效）
_BOOSTER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:8[kK]|4[kK]|masterpiece|award-winning)"
    r"(?![A-Za-z0-9])|杰作|获奖作品|获奖大作")


def heal_image_prompt(text: str):
    """图像提示词机械 heal（官方 validate_prompt.py 移植，2026-10-01）：
    代码围栏剥离 → 换行折叠单段 → 禁词剥离 → 空白收敛；引号不平衡返回
    None（调用方回落原文——官方校验同款判废）。纯函数零 LLM。"""
    if not (text or "").strip():
        return text
    t = text.strip()
    # 代码围栏（小模型常见毛病）：```...``` 整壳剥掉留内核
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    t = re.sub(r"[\r\n]+", " ", t)          # 单段连续（官方规范）
    t = _BOOSTER_RE.sub("", t)               # 禁空词
    t = re.sub(r"[ \t]+", " ", t).strip()
    if t.count('"') % 2 != 0:                # 引号不平衡=画内文字协议已破
        return None
    return t


def _expand_system_for(db, mode: str) -> str:
    """模式 → 系统词。edit 恒中文（官方规则：编辑正文随用户语言）；t2i 随
    comfy.prompt_expand_lang 旋钮（zh 默认/en——用户两版真机 A/B 用）。"""
    if mode == "edit":
        return EXPAND_EDIT_ZH
    lang = str((get_setting(db, "comfy") or {}).get("prompt_expand_lang")
               or "zh").lower()
    return EXPAND_T2I_EN if lang == "en" else EXPAND_T2I_ZH


def expand_image_prompt(db, prompt: str, task: str = "optimize_prompt",
                        mode: str = "t2i") -> str:
    """提交前 LLM 扩写（2026-09-22 用户实测判例：Qwen-Image 2.1 短提示出不了
    好效果）。mode 取模板 manifest prompt_expand 值（t2i/edit——官方扩写
    规范 2026-10-01 接入）。锚定保全是硬约束（系统词明令逐字保留）；任何
    失败回落原文不炸生成——扩写是增强不是门槛。
    **即用即停**（OOM 判例预防）：扩写发生在 gpu_comfy 任务内部（如
    gen_comic_page），任务开始时的让位检查已过——扩写拉起的 llama 若不关，
    随后 ComfyUI 加载 Qwen2.1（~9G）+ llama（5.6~10.9G）必爆 12G 卡。"""
    if not (prompt or "").strip():
        return prompt
    try:
        from .llm.provider import client_for_task
        client = client_for_task(db, task)
        text, _ = client.raw_chat(
            [{"role": "system", "content": _expand_system_for(db, mode)},
             {"role": "user", "content": prompt}], temperature=0.5)
        text = heal_image_prompt((text or "").strip())
        if text is None:
            return prompt                     # 引号不平衡=扩写输出判废，回落
        return text if len(text) >= len(prompt) * 0.6 else prompt  # 过短=丢内容，回落
    except Exception:
        return prompt
    finally:
        try:
            from .llm.local import stop_llama_servers
            stop_llama_servers(db)  # 幂等：没在跑零开销
        except Exception:
            pass
