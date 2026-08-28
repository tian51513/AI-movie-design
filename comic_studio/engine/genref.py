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
            s += "，" + value
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
        from .era import ERA_SUFFIX
        prompt += "。" + ERA_SUFFIX.format(era=era)
    prompt += ZIMAGE_TAIL.get(kind, "")  # Turbo 质量与正向纠错尾缀
    if kind == "character" and variant != "main":
        prompt += "。严格三视图布局：正面、左侧、背面各一个，禁止视角重复"  # 结构收尾再强调
    ctx = {"project": f"p{asset_row['source_project']}", "asset": str(asset_row["id"])}
    return prompt, ctx


def _t2i_to_file(db, comfy, tmpl, prompt, dest, ctx, job, label, images=None):
    """单段 t2i：组工作流 → 提交 → 等待 → 下载到 dest（主图与回退路径共用）。
    images：模板声明图片槽时传入（如文+图重绘的 ref 槽）。"""
    if comfy is None:
        raise RuntimeError("gen_ref 需要 ComfyUI 端点（settings.comfy.base_url）")
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": random.randint(0, 2**31 - 1)},
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
            # 有主图 → 作 ref 传入重绘；无主图 → 引导用纯文生图（zimage_t2i）
            main_tmpl = resolve_template(db, "t2i")
            main_images = None
            if main_tmpl.inject_images:
                if main_png.exists():
                    main_images = [{"slot": main_tmpl.inject_images[0]["slot"],
                                    "path": str(main_png)}]
                else:
                    from .workflows import registry as _reg
                    boot = _reg.scan_templates(_reg.TEMPLATE_ROOT).get("zimage_t2i")
                    if boot is None or boot.inject_images:
                        raise ValueError(
                            f"主图模板 {main_tmpl.id} 需要图片输入，且无现有主图可传"
                            f"（可先把 t2i 映射切回纯文生图模板生成首张主图）")
                    main_tmpl = boot
                    emit_log(db, "comfy", "info",
                             f"无现有主图，引导用纯文生图 {boot.id} 生成首张主图",
                             project_id=job["project_id"], job_id=job["id"])
            _t2i_to_file(db, comfy, main_tmpl, main_prompt, main_png, ctx, job,
                         label=f"资产「{asset['name']}」主图", images=main_images)
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
        _t2i_to_file(db, comfy, _tmpl, prompt, dest, ctx, job,
                     label=f"资产「{asset['name']}」参考图")
    if stage == "main":
        return  # 仅换主图：sheet 未变，无需 stale 联动
    from .shots import mark_stale_for_asset
    n = mark_stale_for_asset(db, asset["id"])
    if n:
        emit_log(db, "storyboard", "warn",
                 f"资产「{asset['name']}」参考图已更新：{n} 个引用它的分镜标记为 stale",
                 project_id=job["project_id"], job_id=job["id"])
