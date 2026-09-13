# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.llm.provider import client_for_task
from ..engine.logbus import emit as emit_log
from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

GEN_STORY_SYSTEM = """你是漫剧改编用的小说作者。按给定主题写一部适合改编为漫画短剧的小说正文：
- 目标 {target_words}，多个具体场景（利于拆分镜），人物有名字与外形特征
- 以画面感写作：动作、表情、环境光线、对白（对白自然口语化）
- 段落之间用空行分隔；不写章节标题、目录、作者注或任何解释
- 直接输出正文本身。

短剧结构规范（借鉴短剧厂方法论，2026-08-29 第三批）：
- 黄金开头：正文前三句必须是强钩子——生死绝境/极端羞辱地位反差/身份暴跌或
  狂飙/物证背叛/反常理悬念任选其一；严禁"早晨拉窗帘"、大段环境铺陈、
  咖啡厅闲聊式开场
- 情绪流变：每场景有清晰的情绪链（如 遭受刁难→隐忍试探→强力反转→悬念收束），
  相邻场景情绪基调不重复
- 断章卡点：每个大场景结尾留在悬念上（真相将揭未揭/危机悬停/金句落地），
  严禁把反转爽完再收尾
- 对白语速：台词总量按 3.5~4.5 字/秒估算，单句常规 12~18 字、金句 8~12 字、
  不超 25 字；对白之间穿插动作与微表情留白（生理可观测动作如指节泛白、
  瞳孔骤缩，禁抽象心理词）
- 禁反向灌输：角色不口头互念双方已知的背景设定，信息用物证/动作带出"""

# 默认目标字数（不传 word_count 时）；可传 word_count 控制（真机 2026-08-27：
# 主题生成 21862 字正文 → 分镜分块过大撞上下文截断，源头控制篇幅）
DEFAULT_STORY_WORDS = "8000~12000 字"
WORD_COUNT_RANGE = (300, 20000)

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at", "style", "style_vis", "era", "comic_mode", "subtitles",
                    "video_megapixels", "video_multiple", "video_speed", "default_shot_duration",
                    "prompt_mode", "lora_realism", "target_duration", "autopilot",
                    "redraw_characters", "redraw_done",
                    # 迁移 36（2026-09-12 小说转漫画）：前端详情页消费
                    "dialogue_mode", "target_pages", "image_size", "quality_tier",
                    # 迁移 37（2026-09-13 气泡渲染）：样式三参数 JSON
                    "bubble_style",
                    # 迁移 38（2026-09-13 B1）：资产停等确认标记
                    "comic_assets_confirmed",
                    # 迁移 39（2026-09-13 二期）：主图停等检查标记
                    "comic_refs_done",
                    # 迁移 40（2026-09-13）：双角色处理模式 stitch/chain
                    "comic_dual_mode")


@router.post("/{project_id}/retry-transcribe", status_code=202)
def retry_transcribe(request: Request, project_id: int):
    """P10 转写手动重发（2026-09-05 真机：interrupted/failed 后此前只能删项目
    重传）。源音频在即可重发；在飞 409。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.asr import load_segments
    from ..engine.paths import data_to_abs as _dta
    adir = _dta(request.app.state.data_dir, f"projects/{proj['slug']}/audio")
    src = next(adir.glob("source.*"), None) if adir.is_dir() else None
    if src is None:
        raise HTTPException(422, "项目无源音频（非有声书项目？）")
    if load_segments(request.app.state.data_dir, proj["slug"]) is not None:
        raise HTTPException(409, "转写已完成（segments 在盘）——无需重发")
    from ..engine import jobs as jobs_mod
    act = jobs_mod.latest_job(db, project_id, "transcribe")
    if act and act["status"] in ("pending", "running"):
        raise HTTPException(409, "转写已在队列/进行中")
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "transcribe", project_id=project_id,
                      payload={"project_id": project_id,
                               "ext": src.suffix.lstrip(".") or "mp3"})
    return {"job_id": jid}


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: int):
    """删除项目：行（jobs/shots/关联/日志/项目）+ 磁盘 projects/<slug>/ 全清；
    在跑渲染发 interrupt；全局资产库（data/library）保留——其他项目可能复用。"""
    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    # 温和停止（2026-09-05 用户决策 A）：删除项目不再 interrupt ComfyUI
    #（部分版本 interrupt 崩实例）——在跑任务写盘报错自然落 failed
    conn = db.connect()
    try:
        # 删除顺序按外键依赖：logs(job_id→jobs) 先于 jobs；
        # jobs(shot_id→shots) 先于 shots；shots 自引用链按叶子序（见下）
        conn.execute("DELETE FROM logs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        # 镜间接力链：先删叶子（无人 depends_on 它的镜）再循环——单条 DELETE 会被
        # 自引用 FK 逐行检查卡住（真机 2026-08-25 Internal Server Error）
        for _ in range(1000):
            cur = conn.execute(
                "DELETE FROM shots WHERE project_id=? AND id NOT IN ("
                "SELECT depends_on FROM shots WHERE project_id=? AND depends_on IS NOT NULL)",
                (project_id, project_id))
            if cur.rowcount == 0:
                break
        # 全局资产保留（library 跨项目复用），仅清来源引用
        conn.execute("UPDATE assets SET source_project=NULL WHERE source_project=?",
                     (project_id,))
        for sql in ("DELETE FROM project_assets WHERE project_id=?",
                    "DELETE FROM projects WHERE id=?"):
            conn.execute(sql, (project_id,))
        conn.commit()
    except Exception:
        conn.rollback()  # 失败必须回滚——否则持锁把 worker 线程锁死（真机教训）
        raise
    import shutil
    shutil.rmtree(Path(request.app.state.data_dir) / "projects" / row["slug"],
                  ignore_errors=True)
    # L9（2026-09-05 审计低危）：卡死上报键顺手清（防模块级 dict 泄漏）
    from ..engine.autopilot import _STUCK_REPORTED
    _STUCK_REPORTED.pop(project_id, None)
    return {"deleted": project_id}


def _theme_story_common(db, body) -> tuple:
    """from-theme 预览/创建共用：校验参数、定位主题。返回 (theme, aspect)。"""
    from ..engine.themes import list_themes
    tid = body.get("theme_id")
    theme = next((t for t in list_themes(db) if t["id"] == tid), None)
    if theme is None:
        raise HTTPException(404, f"主题不存在: {tid}")
    aspect = body.get("aspect_ratio") or "9:16"
    from ..engine.projects import ASPECT_RATIOS
    if aspect not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    return theme, aspect


def _generate_story_text(db, theme, body) -> str:
    """按主题生成正文（预览与直建共用）。用户补充描述（extra_prompt）拼进上下文。"""
    protagonist = (body.get("protagonist") or "").strip()
    extra = (body.get("extra_prompt") or "").strip()
    if len(extra) > 2000:
        raise HTTPException(422, f"补充描述过长（{len(extra)} 字，上限 2000）")
    word_count = body.get("word_count")
    if word_count is not None:
        try:
            word_count = int(word_count)
        except (TypeError, ValueError):
            raise HTTPException(422, "word_count 需为整数")
        lo, hi = WORD_COUNT_RANGE
        if not lo <= word_count <= hi:
            raise HTTPException(422, f"word_count 需在 {lo}~{hi} 之间")
    target = DEFAULT_STORY_WORDS if word_count is None else \
        f"约 {word_count} 字（允许上下浮动 20%）"
    system = GEN_STORY_SYSTEM.format(target_words=target)
    user = (f"主题《{theme['name']}》（{theme['category']}）：{theme['description']}")
    if protagonist:
        user += f"\n主角姓名用「{protagonist}」。"
    if extra:
        user += f"\n用户补充要求（务必体现）：{extra}"
    client = client_for_task(db, "gen_story")
    text, _u = client.raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.7)
    text = (text or "").strip()
    # P7-G 敏感词机械转译（借鉴短剧厂替换库）：修复而非拦截，预览里可见可改
    from ..engine.textfix import apply_sensitive_replacements
    text, n_fixed = apply_sensitive_replacements(text)
    if n_fixed:
        emit_log(db, "llm", "info", f"生成正文敏感词转译 {n_fixed} 处", project_id=None)
    if len(text) < 500:
        raise HTTPException(422, f"生成的正文过短（{len(text)} 字），请重试或换主题")
    return text


@router.post("/from-comic", status_code=201)
def create_from_comic(request: Request,
                      name: str = Form(...), aspect_ratio: str = Form("9:16"),
                      comic_mode: str = Form("motion_comic"),
                      default_shot_duration: float = Form(0.0),
                      target_duration: float = Form(0.0),
                      style: str = Form(""), style_vis: str = Form(""),
                      subtitles: bool = Form(False),  # 漫画默认不烧（原页自带台词）
                      redraw: bool = Form(False),  # 动态漫角色重绘（迁移 35）——漫改忽略
                      video_megapixels: float = Form(0.4),
                      video_multiple: int = Form(32), video_speed: str = Form("标准"),
                      images: list[UploadFile] = File(...)):
    """P8 漫画导入：每图一镜，直达分镜就绪。comic_mode：
    motion_comic（动态漫/fl2v）| film_adaptation（漫改/ref2va）。
    style/style_vis（2026-09-06）：漫改模式的画风转换目标，随创建提交。"""
    if comic_mode not in ("motion_comic", "film_adaptation"):
        comic_mode = "motion_comic"
    blobs = []
    for f in images:
        data = f.file.read()
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(422, f"{f.filename} 超过 20MB 上限")
        blobs.append((f.filename or "page.png", data))
    try:
        from ..engine.comic import import_comic
        proj = import_comic(request.app.state.db, request.app.state.data_dir,
                            name, aspect_ratio, blobs, comic_mode=comic_mode,
                            default_shot_duration=default_shot_duration,
                            target_duration=target_duration,
                            style=style, style_vis=style_vis,
                            subtitles=1 if subtitles else 0,
                            # 重绘仅动态漫语义（漫改画风本就要转换，恒 0）
                            redraw_characters=1 if (redraw and comic_mode == "motion_comic") else 0,
                            video_megapixels=video_megapixels,
                            video_multiple=video_multiple, video_speed=video_speed)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _public(proj)


def _validate_comic_output_params(dialogue_mode, target_pages, image_size, quality_tier):
    """comic_output 四参数统一 422 校验（from-comic-novel / from-comic-audio 共用）。"""
    if dialogue_mode not in ("bubble", "footer", "none"):
        raise HTTPException(422, "dialogue_mode 只能是 bubble（气泡）/footer（底部字幕条）/none（不呈现）")
    if quality_tier not in ("fast", "standard", "high"):
        raise HTTPException(422, "quality_tier 只能是 fast/standard/high")
    from ..engine.comicgen import MP_TIERS, SIZE_PRESETS  # 惰性：不拉注册链
    if image_size not in MP_TIERS and image_size not in SIZE_PRESETS:
        raise HTTPException(422, f"image_size 只能是百万像素档 {'/'.join(MP_TIERS)}"
                                 f"（旧 WxH 值兼容）")
    if target_pages < 0 or target_pages > 200:
        raise HTTPException(422, "target_pages 需在 0~200（0=按剧情密度自动）")


def _validate_bubble_style(raw) -> str:
    """bubble_style JSON 422 校验（2026-09-13 气泡渲染）：需为对象；opacity
    0~100、font_size ≥0。合法返回原样字符串（引擎读取时补默认，存原样
    保证「只写 opacity」的形状不被默认值撑爆）。"""
    import json as _json
    try:
        d = _json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(422, "bubble_style 需为 JSON 对象，如 {\"opacity\":85}")
    if not isinstance(d, dict):
        raise HTTPException(422, "bubble_style 需为 JSON 对象")
    if "opacity" in d and not (isinstance(d["opacity"], (int, float))
                               and 0 <= d["opacity"] <= 100):
        raise HTTPException(422, "opacity 需在 0~100（百分比，只作用气泡底色）")
    if "font_size" in d and not (isinstance(d["font_size"], (int, float))
                                 and d["font_size"] >= 0):
        raise HTTPException(422, "font_size 需 ≥0（0=随页宽自适应）")
    return raw


@router.post("/from-comic-novel", status_code=201)
def create_from_comic_novel(request: Request,
                            name: str = Form(...),
                            aspect_ratio: str = Form("9:16"),
                            novel: UploadFile = File(None),
                            text: str = Form(""),
                            style: str = Form(""), style_vis: str = Form(""),
                            dialogue_mode: str = Form("bubble"),
                            target_pages: int = Form(0),
                            image_size: str = Form("0.8"),
                            quality_tier: str = Form("standard"),
                            bubble_style: str = Form(""),
                            video_megapixels: float = Form(0.4),
                            video_multiple: int = Form(32),
                            video_speed: str = Form("标准"),
                            default_shot_duration: float = Form(0.0),
                            target_duration: float = Form(0.0)):
    """小说转漫画创建（2026-09-12 Task 6）：正文上传 + 漫画输出四参数 →
    comic_output 项目，之后走既有 分析→拆分镜→autopilot 逐页 t2i
    （页面即交付物，无视频渲染链）。subtitles 恒 0——对白由页面自呈
    （气泡/底部字幕条/不呈现），视频字幕烧录对漫画无意义。

    2026-09-13 Part A：正文来源两选一——novel 文件（文本小说 tab）或
    text 直传（主题生成第二步：preview 已出正文，不再落临时文件）；文件优先。"""
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    _validate_comic_output_params(dialogue_mode, target_pages, image_size, quality_tier)
    if bubble_style:
        bubble_style = _validate_bubble_style(bubble_style)
    if novel is not None:
        try:
            novel_text = novel.file.read().decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "小说文件需为 UTF-8 编码（请转换后重新上传）")
    elif (text or "").strip():
        novel_text = text
    else:
        raise HTTPException(422, "正文缺失：上传 .txt 文件或 text 二选一（主题生成直传）")
    proj = create_project(request.app.state.db, request.app.state.data_dir,
                          name, aspect_ratio, novel_text, style=style, style_vis=style_vis,
                          comic_mode="comic_output",
                          dialogue_mode=dialogue_mode, target_pages=target_pages,
                          image_size=image_size, quality_tier=quality_tier,
                          bubble_style=bubble_style,
                          video_megapixels=video_megapixels,
                          video_multiple=video_multiple, video_speed=video_speed,
                          default_shot_duration=default_shot_duration,
                          target_duration=target_duration,
                          subtitles=0)
    return _public(proj)


@router.post("/from-comic-audio", status_code=201)
def create_from_comic_audio(request: Request, name: str = Form(...),
                            aspect_ratio: str = Form("9:16"),
                            style: str = Form(""), style_vis: str = Form(""),
                            dialogue_mode: str = Form("bubble"),
                            target_pages: int = Form(0),
                            image_size: str = Form("0.8"),
                            quality_tier: str = Form("standard"),
                            bubble_style: str = Form(""),
                            video_megapixels: float = Form(0.4),
                            video_multiple: int = Form(32), video_speed: str = Form("标准"),
                            default_shot_duration: float = Form(0.0),
                            target_duration: float = Form(0.0),
                            audio: UploadFile = File(...)):
    """漫画项目·有声小说入口（2026-09-13 Part A）：存源音频 → 建
    comic_output 项目（占位正文+四漫画参数，subtitles 恒 0）→ 入队
    transcribe；转写回填正文后 autopilot 按 comic_mode 自动走漫画链
    （分析→拆解漫画分支→逐页→comic_ready）——引擎侧零改动，复用 P10 全链。"""
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    _validate_comic_output_params(dialogue_mode, target_pages, image_size, quality_tier)
    if bubble_style:
        bubble_style = _validate_bubble_style(bubble_style)
    data = audio.file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(422, "音频超过 200MB 上限（请先切分）")
    ext = (Path(audio.filename or "a.mp3").suffix.lstrip(".") or "mp3").lower()
    if ext not in ("mp3", "wav", "m4a", "flac", "ogg"):
        raise HTTPException(422, f"不支持的音频格式 .{ext}")
    from ..engine.asr import TranscribeUnavailable  # noqa: 探测依赖
    try:
        from faster_whisper import WhisperModel  # noqa: F401 —— 缺包即 422
    except (ImportError, TranscribeUnavailable) as e:  # 破损安装抛普通 ImportError
        raise HTTPException(422, f"ASR 依赖未安装：{e}；"
                                 "WSL `.venv/bin/pip install -e '.[asr]'` / "
                                 "Windows `.venv-win/Scripts/pip.exe install -e '.[asr]'`")
    proj = create_project(request.app.state.db, request.app.state.data_dir,
                          name, aspect_ratio, "（有声书转写中，转写完成后自动回填正文）",
                          style=style, style_vis=style_vis,
                          comic_mode="comic_output",
                          dialogue_mode=dialogue_mode, target_pages=target_pages,
                          image_size=image_size, quality_tier=quality_tier,
                          bubble_style=bubble_style,
                          video_megapixels=video_megapixels,
                          video_multiple=video_multiple, video_speed=video_speed,
                          default_shot_duration=default_shot_duration,
                          target_duration=target_duration,
                          subtitles=0)
    adir = Path(request.app.state.data_dir) / f"projects/{proj['slug']}/audio"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"source.{ext}").write_bytes(data)
    from ..engine.jobs import enqueue_job
    enqueue_job(request.app.state.db, "transcribe", project_id=proj["id"],
                payload={"project_id": proj["id"], "ext": ext})
    return _public(proj)


@router.post("/{project_id}/asr-cleanup", status_code=202)
def asr_cleanup_route(request: Request, project_id: int, body: dict | None = None):
    """P10C 转写校对（2026-09-06 队列化）：入队 gpu_llm_local 与渲染互斥
    （同步 HTTP 时代绕过资源组抢显存）。theme 留空自动读项目持久化主题。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.asr import load_segments
    from ..engine.paths import data_to_abs as _dta
    if not load_segments(request.app.state.data_dir, proj["slug"]):
        raise HTTPException(409, "无转写段落（非音频项目或转写未完成）")
    body = body or {}
    theme = str(body.get("theme") or "").strip()
    if not theme:
        tf = _dta(request.app.state.data_dir,
                  f"projects/{proj['slug']}/audio/theme.txt")
        if tf.exists():
            theme = tf.read_text(encoding="utf-8").strip()
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "asr_cleanup", project_id=project_id,
                      resource="gpu_llm_local",
                      payload={"project_id": project_id, "theme": theme,
                               "mode": str(body.get("mode") or "conservative")})
    return {"job_id": jid}


@router.get("/{project_id}/novel-text")
def novel_text(request: Request, project_id: int):
    """2026-09-05 用户需求：详情页查看正文（上传小说/音频转写通用）。
    from_audio=源音频在盘（转写正文）；文件缺失 404。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.paths import data_to_abs
    f = data_to_abs(request.app.state.data_dir, proj["novel_path"])
    if not f.exists():
        raise HTTPException(404, "正文文件缺失（转写未完成或已删除）")
    text = f.read_text(encoding="utf-8")
    adir = data_to_abs(request.app.state.data_dir,
                       f"projects/{proj['slug']}/audio")
    from_audio = adir.is_dir() and next(adir.glob("source.*"), None) is not None
    return {"text": text, "char_count": len(text), "from_audio": from_audio}


@router.put("/{project_id}/novel-text")
def novel_text_edit(request: Request, project_id: int, body: dict):
    """2026-09-06 用户需求：正文人工校正——LLM 校正外的第二条路，LLM 校正后
    也可继续手改。写回 novel.txt + 重算章节；segments（音频时长锚）不动——
    正文大改后拆镜对不齐的段落回落估时（与 free 模式同语义）。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "正文不能为空")
    from ..engine.chapters import parse_chapters
    from ..engine.paths import data_to_abs
    f = data_to_abs(request.app.state.data_dir, proj["novel_path"])
    if not f.exists():
        raise HTTPException(404, "正文文件缺失（转写未完成或已删除）")
    f.write_text(text, encoding="utf-8")
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(text), ensure_ascii=False), project_id))
    conn.commit()
    emit_log(db, "asr", "info", f"人工校正正文：{len(text)} 字", project_id=project_id)
    # 2026-09-06 有声2 教训第二入口：写回后分镜若已存在即脱节——明示重拆
    n_shots = conn.execute(
        "SELECT COUNT(*) FROM shots WHERE project_id=?", (project_id,)).fetchone()[0]
    if n_shots:
        emit_log(db, "asr", "warn",
                 f"正文已更新，但已有 {n_shots} 镜分镜基于旧文本——"
                 "请重拆分镜后重生成提示词", project_id=project_id)
    return {"char_count": len(text)}


@router.post("/from-audio", status_code=201)
def create_from_audio(request: Request, name: str = Form(...),
                      aspect_ratio: str = Form("9:16"),
                      default_shot_duration: float = Form(0.0),
                      target_duration: float = Form(0.0),
                      subtitles: bool = Form(True),
                      video_megapixels: float = Form(0.4),
                      video_multiple: int = Form(32), video_speed: str = Form("标准"),
                      render_mode: str = Form(""),
                      audio: UploadFile = File(...)):
    """P10 有声书导入（2026-09-05 计划）：存源音频 → 建项目（占位正文）→
    入队 transcribe；转写完成后回填 novel.txt，之后走现有小说链。"""
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    data = audio.file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(422, "音频超过 200MB 上限（请先切分）")
    ext = (Path(audio.filename or "a.mp3").suffix.lstrip(".") or "mp3").lower()
    if ext not in ("mp3", "wav", "m4a", "flac", "ogg"):
        raise HTTPException(422, f"不支持的音频格式 .{ext}")
    from ..engine.asr import TranscribeUnavailable  # noqa: 探测依赖
    try:
        from faster_whisper import WhisperModel  # noqa: F401 —— 缺包即 422
    except (ImportError, TranscribeUnavailable) as e:  # 破损安装抛普通 ImportError
        raise HTTPException(422, f"ASR 依赖未安装：{e}；"
                                 "WSL `.venv/bin/pip install -e '.[asr]'` / "
                                 "Windows `.venv-win/Scripts/pip.exe install -e '.[asr]'`")
    proj = create_project(request.app.state.db, request.app.state.data_dir,
                          name, aspect_ratio, "（有声书转写中，转写完成后自动回填正文）",
                          default_shot_duration=default_shot_duration,
                          target_duration=target_duration,
                          subtitles=1 if subtitles else 0,
                          video_megapixels=video_megapixels,
                          video_multiple=video_multiple, video_speed=video_speed)
    adir = Path(request.app.state.data_dir) / f"projects/{proj['slug']}/audio"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"source.{ext}").write_bytes(data)
    from ..engine.jobs import enqueue_job
    enqueue_job(request.app.state.db, "transcribe", project_id=proj["id"],
                payload={"project_id": proj["id"], "ext": ext})
    return _public(proj)


@router.post("/{project_id}/extract-comic-characters", status_code=202)
def extract_comic_characters_route(project_id: int, request: Request):
    """P8-B 漫改模式：VLM 读前几页提取角色 → 建资产（队列任务）。
    终审 2026-09-09：动态漫+开重绘项目（按钮在此可见）必须携带重绘链键
    characters_only/bind_shots——与 autopilot extract_comic 分支同语义，
    缺键=重绘项目被当漫改全量提取（建场景/道具、不绑上镜）。"""
    db = request.app.state.db
    from ..engine.projects import get_project
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    payload = {"project_id": project_id}
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") == "motion_comic" \
            and "redraw_characters" in proj.keys() and proj["redraw_characters"]:
        payload["characters_only"] = True
        payload["bind_shots"] = True
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "extract_comic_characters", project_id=project_id,
                      resource="gpu_llm_local", payload=payload)
    return {"job_id": jid}


@router.post("/{project_id}/reestimate-durations")
def reestimate_durations_route(project_id: int, request: Request):
    """优化#4（2026-09-07）：只重估对白镜时长（字数基准），不重读图不烧 VLM——
    修正时长/换 TTS 后重算片长用。即时同步（无 LLM 调用）。"""
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.comic import reestimate_project_durations
    return {"reestimated": reestimate_project_durations(db, project_id)}


@router.post("/{project_id}/describe-shots", status_code=202)
def describe_shots_route(project_id: int, request: Request,
                         shot_id: int = 0, force: bool = False):
    """P8 VLM 读图生成提示词（队列任务化，2026-08-29 用户需求：
    队列状态可见）。shot_id>0 单镜；否则批量跑全部空提示词镜；
    force=true 批量也覆盖已有提示词（2026-09-06 改画风后整批重读）。"""
    db = request.app.state.db
    from ..engine.projects import get_project
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.pipeline_jobs import enqueue_llm_job
    payload = {"project_id": project_id}
    if shot_id:
        payload["shot_id"] = shot_id
    if force:
        payload["force"] = True
    # QC-E：按 routing 定资源（此前硬编码 gpu_llm_local，配 online 时过度互斥）
    jid = enqueue_llm_job(db, "describe_shots", project_id=project_id,
                          shot_id=shot_id if shot_id else None,
                          payload=payload)
    return {"job_id": jid}


@router.post("/{project_id}/confirm-comic-assets", status_code=202)
def confirm_comic_assets(request: Request, project_id: int):
    """B1 漫画链资产停等确认（2026-09-13）：用户检查/改名/删减名册后放行——
    落 comic_assets_confirmed=1。二期（同日）后确认不再直进 assets_ready：
    autopilot 接管「生成角色主图 → 停等检查 → confirm-comic-refs」——
    确认路由直接 set assets_ready 会绕过主图阶段（页面注入参考源）。幂等。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）有资产确认环节")
    if not (proj["comic_assets_confirmed"]
            if "comic_assets_confirmed" in proj.keys() else 0):
        conn = db.connect()
        conn.execute("UPDATE projects SET comic_assets_confirmed=1 WHERE id=?",
                     (project_id,))
        # 「继续出片」语义兑现：确认即自动开一键出片（真机验收 2026-09-13——
        # 手动流点确认后主图不动，「要人工点哪个角色吗」困惑根因）
        conn.execute("UPDATE projects SET autopilot=1 WHERE id=? AND autopilot=0",
                     (project_id,))
        conn.commit()
        emit_log(db, "autopilot", "info", "资产名册已确认，进入主图生成（已开启一键出片）",
                 project_id=project_id)
    return {"confirmed": True}


@router.post("/{project_id}/rebubble-comic")
def rebubble_comic(request: Request, project_id: int, body: dict | None = None):
    """对白重排（2026-09-13 用户建议·干净副本）：改 dialogue_mode/气泡样式后
    从 page_NNN_clean.png 本地重排（Pillow 秒级）——不重出图不烧 ComfyUI、
    画面零变化。无干净副本的页（旧页）跳过并计数（重出一次即有备份）。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）可重排对白")
    from ..engine.comicgen import _postprocess_dialogue, _bubble_style
    from ..engine.paths import data_to_abs
    from ..engine.shots import list_shots
    pages_dir = data_to_abs(request.app.state.data_dir,
                            f"projects/{proj['slug']}/pages")
    mode = proj["dialogue_mode"] or "bubble"
    style = _bubble_style((proj["bubble_style"] if "bubble_style" in proj.keys() else "") or "")
    import json as _json
    body = body or {}
    only = set(body.get("shot_ids") or [])
    reset_pos = bool(body.get("reset_positions"))
    n = skipped = 0
    for s in list_shots(db, project_id):
        if s["disabled"] or (only and s["id"] not in only):
            continue
        clean = pages_dir / f"page_{s['seq']:03d}_clean.png"
        active = pages_dir / f"page_{s['seq']:03d}.png"
        if not clean.exists() or not active.exists():
            skipped += 1
            continue
        active.write_bytes(clean.read_bytes())
        led = _json.loads(s["ledger_json"] or "{}")
        pos = led.get("bubble_pos")
        if reset_pos and pos:
            led.pop("bubble_pos", None)
            from ..engine.shots import update_shot as _us
            _us(db, s["id"], {"ledger_json": _json.dumps(led, ensure_ascii=False)})
            pos = None
        _postprocess_dialogue(active, led.get("dialogue") or [], mode, style=style,
                              positions=pos)
        n += 1
    emit_log(db, "comic", "info",
             f"对白重排完成：{n} 页（{mode} 模式）"
             + (f"，{skipped} 页无干净副本跳过（重出一次即有）" if skipped else ""),
             project_id=project_id)
    return {"rebubbled": n, "skipped": skipped}


@router.post("/{project_id}/convert-to-video")
def convert_to_video(request: Request, project_id: int, body: dict | None = None):
    """漫画→视频转化（2026-09-13 用户需求）：comic_ready 后变身视频项目，
    复用全链数据——分镜/对白（ledger）原样、角色资产+主图白捡、正文/章节在。
    提示词策略（用户决策）：不 VLM 读图，清空后由 gen_prompts 从分镜描述生成
    H3 视频提示词（对白本在 ledger 不丢）；提示词不行可手动「🔄 强制重读」。
    漫画页保留 pages/（可回看/导出 PDF）。

    body 可选视频参数（用户需求：同漫画导入设置面板，画风缺省继承漫画项目）：
    video_megapixels/video_multiple/video_speed/prompt_mode/subtitles/
    render_mode（覆写全部镜 workflow_type）/lora_realism/default_shot_duration/
    target_duration/aspect_ratio。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）可转视频（或已转化）")
    if proj["stage"] != "comic_ready":
        raise HTTPException(409, f"阶段 {proj['stage']} 不能转视频（需漫画就绪）")
    body = body or {}
    # 转化类型（2026-09-13 用户决策）：standard=重渲染（资产参考+提示词全新画面）/
    # motion=动态漫（漫画原画动起来——活动页换干净副本去气泡，首尾帧源=原画）/
    # film=漫改电影（角色动画）；字幕恒 1（对白走 TTS+字幕，不靠帧内气泡）
    video_mode = body.get("video_mode", "standard")
    if video_mode not in ("standard", "motion", "film"):
        raise HTTPException(422, "video_mode 只能是 standard（重渲染）/motion（动态漫）/film（漫改电影）")
    _mode_map = {"standard": "", "motion": "motion_comic", "film": "film_adaptation"}
    # 视频参数先过统一校验（非法 422；合法经 update_video_params 落列）
    vp_keys = ("video_megapixels", "video_multiple", "video_speed", "prompt_mode",
               "lora_realism", "default_shot_duration", "target_duration",
               "aspect_ratio")
    kwargs = {k: body[k] for k in vp_keys if k in body}
    wf_mode = body.get("render_mode", "")
    if wf_mode and wf_mode not in ("ref2va", "fl2v", "t2v"):
        raise HTTPException(422, "render_mode 只能是 ref2va/fl2va/t2v")
    conn = db.connect()
    conn.execute(
        "UPDATE projects SET comic_mode=?, subtitles=?, autopilot=1 WHERE id=?",
        (_mode_map[video_mode], 1 if body.get("subtitles", True) else 0,
         project_id,))
    # 画风覆盖（转化面板预填漫画项目画风·可改）：style_vis 与旧 style 相等
    # （未拆层）时同步跟随；独立拆层过则不动（视频提示词仍吃独立子集）
    new_style = (body.get("style") or "").strip()
    if new_style and new_style != (proj["style"] or ""):
        _vis = (proj["style_vis"] or "")
        if not _vis or _vis == (proj["style"] or ""):  # 空/未拆层 → 同步
            conn.execute("UPDATE projects SET style=?, style_vis=? WHERE id=?",
                         (new_style, new_style, project_id))
        else:
            conn.execute("UPDATE projects SET style=? WHERE id=?",
                         (new_style, project_id))
    conn.commit()
    if kwargs:
        from ..engine.projects import update_video_params
        try:
            update_video_params(db, project_id, **kwargs)
        except ValueError as e:
            raise HTTPException(422, str(e))
    # 镜复位：清漫画占位 prompt（走 gen_prompts 重生视频提示词）+
    # workflow_type 'comic'→渲染模式（'comic' 会让视频渲染缺模板炸）；
    # 对白/绑定/时长原样
    conn = db.connect()
    conn.execute(
        "UPDATE shots SET prompt='', workflow_type=?, status='ready' "
        "WHERE project_id=?", (wf_mode or "ref2va", project_id))
    if wf_mode:
        conn.execute("UPDATE projects SET render_mode=? WHERE id=?",
                     (wf_mode, project_id))
    conn.commit()
    if video_mode in ("motion", "film"):
        # 动态漫/漫改：活动页换干净副本（去气泡——首尾帧不带字，对白走
        # TTS+字幕；无副本的页保留原样，后续重出即有）
        from ..engine.paths import data_to_abs as _dta3
        _pd = _dta3(request.app.state.data_dir, f"projects/{proj['slug']}/pages")
        _swapped = 0
        if _pd.is_dir():
            for _cl in sorted(_pd.glob("page_*_clean.png")):
                _act = _pd / _cl.name.replace("_clean.png", ".png")
                if _act.exists():
                    _act.write_bytes(_cl.read_bytes())
                    _swapped += 1
        if _swapped:
            emit_log(db, "autopilot", "info",
                     f"已切换 {_swapped} 页为无气泡干净副本（首尾帧不带字）",
                     project_id=project_id)
    from ..engine.projects import set_stage
    set_stage(db, project_id, "storyboard_ready")
    emit_log(db, "autopilot", "info",
             "已转化为视频项目：分镜描述将生成视频提示词（autopilot 接管渲染→合成），"
             "漫画页保留在 pages/ 可导出", project_id=project_id)
    return _public(get_project(db, project_id))


@router.post("/{project_id}/generate-comic-mains", status_code=202)
def generate_comic_mains(request: Request, project_id: int):
    """手动批量生成角色主图（2026-09-13 验收反馈）：漫画项目主图阶段的手动
    入口——缺 main.png 的角色逐个入队 gen_ref stage=main（与 autopilot 分支
    同口径）；已有/在飞/场景道具跳过。autopilot 开着也不冲突（在飞互斥）。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）有主图批量生成")
    from ..engine.settings import ensure_comfy_configured
    try:
        ensure_comfy_configured(db)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    from ..engine.jobs import enqueue_job
    from ..engine.paths import data_to_abs
    from ..engine.assets import list_project_assets
    n = 0
    queued = {r["asset_id"] for r in db.connect().execute(
        "SELECT DISTINCT asset_id FROM jobs WHERE type='gen_ref' "
        "AND asset_id IS NOT NULL AND status IN ('pending','running')")}
    for a in list_project_assets(db, project_id):
        if a["kind"] != "character" or a["id"] in queued:
            continue
        if (data_to_abs(request.app.state.data_dir, a["library_dir"]) / "main.png").exists():
            continue
        enqueue_job(db, "gen_ref", project_id=project_id, asset_id=a["id"],
                    resource="gpu_comfy", payload={"asset_id": a["id"], "stage": "main"})
        n += 1
    return {"enqueued": n}


@router.post("/{project_id}/confirm-comic-refs", status_code=202)
def confirm_comic_refs(request: Request, project_id: int):
    """二期主图停等放行（2026-09-13）：用户检查角色主图满意后点「✓ 主图满意」
    ——落 comic_refs_done=1 并直进 assets_ready 拆解。幂等。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）有主图确认环节")
    if not (proj["comic_refs_done"]
            if "comic_refs_done" in proj.keys() else 0):
        conn = db.connect()
        conn.execute("UPDATE projects SET comic_refs_done=1 WHERE id=?", (project_id,))
        conn.execute("UPDATE projects SET autopilot=1 WHERE id=? AND autopilot=0",
                     (project_id,))
        conn.commit()
        emit_log(db, "autopilot", "info", "角色主图已确认，进入拆解出页（已开启一键出片）",
                 project_id=project_id)
        if proj["stage"] == "analyzed":
            from ..engine.projects import set_stage
            set_stage(db, project_id, "assets_ready")
    return {"confirmed": True}


@router.post("/{project_id}/generate-comic-pages", status_code=202)
def generate_comic_pages(request: Request, project_id: int,
                         force: bool = False, body: dict | None = None):
    """手动补页/重出（2026-09-12 Task 6；2026-09-13 A 扩 force/shot_ids）：
    缺页镜逐个入队 gen_comic_page——非 autopilot 用户的生成入口，也是
    autopilot「上次漫画页生成失败，重试请手动发起」守卫的落点。

    force=1：已有页也入队（全部重出——改尺寸/画风/对白呈现后用）；
    body.shot_ids：只重出指定镜（单页/所选）。已有页/在飞镜/无效镜跳过
    （force 只解除「已有页」跳过）；handler 每次运行读项目行最新参数、
    同名覆盖 page_NNN.png。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    if (proj["comic_mode"] if "comic_mode" in proj.keys() else "") != "comic_output":
        raise HTTPException(422, "仅漫画成品项目（comic_output）可生成漫画页")
    if proj["stage"] not in ("storyboard_ready", "comic_ready"):
        raise HTTPException(409, f"阶段 {proj['stage']} 不能生成漫画页（需分镜就绪）")
    from ..engine.settings import ensure_comfy_configured
    try:
        ensure_comfy_configured(db)  # 配置门禁（2026-09-01 事故防线同款）
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    from ..engine.jobs import enqueue_job
    from ..engine.paths import data_to_abs
    from ..engine.shots import list_shots
    shots = list_shots(db, project_id)
    only_ids = None
    if body and body.get("shot_ids"):
        only_ids = set(body["shot_ids"])
        own = {s["id"] for s in shots}
        if not only_ids <= own:
            raise HTTPException(422, f"shot_ids 含不属于本项目的镜: {sorted(only_ids - own)}")
    pages_dir = data_to_abs(request.app.state.data_dir,
                            f"projects/{proj['slug']}/pages")
    queued = {r["shot_id"] for r in db.connect().execute(
        "SELECT DISTINCT shot_id FROM jobs WHERE type='gen_comic_page' "
        "AND shot_id IS NOT NULL AND status IN ('pending','running')")}
    n = 0
    for s in shots:
        if s["disabled"] or s["id"] in queued:
            continue
        if only_ids is not None and s["id"] not in only_ids:
            continue
        # 已有页跳过只对「补缺」语义生效——shot_ids（重出所选）与 force 都
        # 是显式覆盖意图（2026-09-13 真机：单卡重出被此拦下 enqueued 0）
        if (only_ids is None and not force
                and (pages_dir / f"page_{s['seq']:03d}.png").exists()):
            continue
        # force/shot_ids=显式重出语义 → 换新 seed（真机判例：库值 seed 优先
        # 让重出输出雷同「没变化」）；autopilot 补缺路径不带 → 连续性 seed 照用
        _pl = {"shot_id": s["id"]}
        if force or only_ids is not None:
            _pl["reshuffle_seed"] = True
        enqueue_job(db, "gen_comic_page", project_id=project_id, shot_id=s["id"],
                    resource="gpu_comfy", payload=_pl)
        n += 1
    return {"enqueued": n}


@router.post("/{project_id}/export-comic")
def export_comic(request: Request, project_id: int, format: str = "pdf"):
    """漫画成书导出（小说转漫画 Task 5）：pages/page_NNN.png → PDF（Pillow
    可选依赖）或竖排长图（ffmpeg）。同步执行（秒级）返回文件路径；
    url=/media/<rel> 前端可直接预览/下载。format=pdf|strip。"""
    db = request.app.state.db
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    if format not in ("pdf", "strip"):
        raise HTTPException(422, "format 只能是 pdf（PDF）或 strip（长图）")
    from ..engine import comicexport
    from ..engine.paths import rel_to_data
    try:
        out = (comicexport.export_comic_pdf if format == "pdf"
               else comicexport.export_comic_strip)(
            db, request.app.state.data_dir, project_id)
    except ValueError as e:  # 无页/Pillow 缺失——显式 422 带安装指引（asr 判例）
        raise HTTPException(422, str(e))
    except RuntimeError as e:  # ffmpeg 拼接失败，stderr 尾段透出供排障
        raise HTTPException(500, str(e))
    rel = rel_to_data(request.app.state.data_dir, out)
    return {"path": str(out), "rel": rel, "url": f"/media/{rel}"}


@router.post("/from-theme/preview")
def preview_from_theme(request: Request, body: dict):
    """两步创建第一步（2026-08-27 需求）：只生成正文给用户确认/编辑，不建项目。"""
    theme, aspect = _theme_story_common(request.app.state.db, body)
    text = _generate_story_text(request.app.state.db, theme, body)
    return {"text": text, "name": (body.get("name") or theme["name"])}


@router.post("/from-theme", status_code=201)
def create_from_theme(request: Request, body: dict):
    """主题建项目。两步流：body 带 text（用户确认/编辑后的正文）则直接建项目不再调 LLM；
    不带 text 则一步到位生成+创建（兼容旧流程）。"""
    db = request.app.state.db
    data_dir = request.app.state.data_dir
    theme, aspect = _theme_story_common(db, body)
    text = (body.get("text") or "").strip()
    if text:
        if len(text) < 100:
            raise HTTPException(422, f"正文过短（{len(text)} 字，至少 100 字）")
    else:
        text = _generate_story_text(db, theme, body)
    row = create_project(db, data_dir, body.get("name") or theme["name"], aspect,
                         text, style=(body.get("style") or ""),
                         style_vis=(body.get("style_vis") or ""),
                         default_shot_duration=float(body.get("default_shot_duration") or 0.0),
                         target_duration=float(body.get("target_duration") or 0.0),
                         video_megapixels=float(body.get("video_megapixels") or 0.4),
                         video_multiple=int(body.get("video_multiple") or 32),
                         video_speed=str(body.get("video_speed") or "标准"),
                         subtitles=1 if body.get("subtitles", True) else 0,
                         render_mode=str(body.get("render_mode") or ""))
    return _public(row)


def _public(row) -> dict:
    return {k: row[k] for k in _PUBLIC_COLUMNS}


@router.post("", status_code=201)
def create(request: Request, name: str = Form(...),
           aspect_ratio: str = Form(...), novel: UploadFile = File(...),
           style: str = Form(""), style_vis: str = Form(""),
           video_megapixels: float = Form(0.4),
           video_multiple: int = Form(32), video_speed: str = Form("标准"),
           default_shot_duration: float = Form(0.0),  # 0=LLM 动态估时（2026-09-05 默认）
           prompt_mode: str = Form("D"), lora_realism: float = Form(0.75),
           target_duration: float = Form(0.0),
           subtitles: bool = Form(True),
           render_mode: str = Form("")):
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    try:
        text = novel.file.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "小说文件需为 UTF-8 编码（请转换后重新上传）")
    row = create_project(request.app.state.db, request.app.state.data_dir,
                         name, aspect_ratio, text, style=style, style_vis=style_vis,
                         video_megapixels=video_megapixels, video_multiple=video_multiple,
                         video_speed=video_speed, default_shot_duration=default_shot_duration,
                         prompt_mode=prompt_mode, lora_realism=lora_realism,
                         target_duration=target_duration,
                         subtitles=1 if subtitles else 0,
                         render_mode=render_mode)
    return _public(row)


@router.get("")
def listing(request: Request):
    db = request.app.state.db
    data_dir = request.app.state.data_dir
    # 富信息（2026-08-27 需求）：最近活动=各表最大时间；摘要/字数读小说文件（本地小文件，量级无虞）
    last_job = {r["project_id"]: r["m"] for r in db.connect().execute(
        "SELECT project_id, MAX(created_at) m FROM jobs GROUP BY project_id")}
    shot_counts = {r["project_id"]: r["c"] for r in db.connect().execute(
        "SELECT project_id, COUNT(*) c FROM shots GROUP BY project_id")}
    out = []
    for r in list_projects(db):
        item = _public(r)
        item["updated_at"] = last_job.get(r["id"]) or r["created_at"]
        item["shot_count"] = shot_counts.get(r["id"], 0)
        try:
            from ..engine.paths import data_to_abs
            text = data_to_abs(data_dir, r["novel_path"]).read_text(encoding="utf-8")
            item["char_count"] = len(text)
            item["excerpt"] = re.sub(r"\s+", " ", text).strip()[:60]
        except OSError:
            item["char_count"], item["excerpt"] = 0, ""
        if r["autopilot"]:
            from ..engine.autopilot import next_action
            item["autopilot_action"] = next_action(db, data_dir, r["id"])
        out.append(item)
    return out


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    row = get_project(request.app.state.db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    out = _public(row)
    if row["autopilot"]:
        from ..engine.autopilot import next_action
        out["autopilot_action"] = next_action(
            request.app.state.db, request.app.state.data_dir, project_id)
    return out


@router.patch("/{project_id}")
def patch_style(request: Request, project_id: int, body: dict):
    from pydantic import BaseModel

    class StylePatch(BaseModel):
        style: str = ""
        style_vis: str = ""

    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")

    # Handle style parameter
    if "style" in body or "style_vis" in body:
        patch = StylePatch.model_validate(body)
        conn = db.connect()
        if "style" in body:
            conn.execute("UPDATE projects SET style=? WHERE id=?", (patch.style.strip(), project_id))
        if "style_vis" in body:
            conn.execute("UPDATE projects SET style_vis=? WHERE id=?",
                         (patch.style_vis.strip(), project_id))
        conn.commit()

    # 字幕开关（迁移 33）：改完「重新合成」即生效，无需重渲染
    if "subtitles" in body:
        conn = db.connect()
        conn.execute("UPDATE projects SET subtitles=? WHERE id=?",
                     (1 if body["subtitles"] else 0, project_id))
        conn.commit()

    # 动态漫角色重绘（迁移 35）：可后开（autopilot 从提取步幂等接入）可后关（不回滚）
    if "redraw_characters" in body:
        conn = db.connect()
        conn.execute("UPDATE projects SET redraw_characters=? WHERE id=?",
                     (1 if body["redraw_characters"] else 0, project_id))
        conn.commit()

    # Handle autopilot switch (一键出片)
    if "autopilot" in body:
        on = 1 if body["autopilot"] else 0
        conn = db.connect()
        conn.execute("UPDATE projects SET autopilot=? WHERE id=?", (on, project_id))
        conn.commit()
        if row["autopilot"] == 1 and on == 0:
            # 2026-09-04 设计A1 暂停联动全停：关开关同时取消 pending/打断 running——
            # 排队的 gen_prompt 不再跑完覆盖用户正要改的提示词（列表/详情/底栏共用此 PATCH）
            from ..engine.jobs import cancel_project_jobs
            from ..engine.logbus import emit as emit_log
            result = cancel_project_jobs(db, project_id)
            emit_log(db, "autopilot", "info",
                     f"停止自动：取消 {result['cancelled']} 个排队任务、"
                     f"停止 {result['stopping']} 个运行中任务", project_id=project_id)

    # Handle render_mode (视频渲染模式项目级切换 → 批量改全部镜 workflow_type；
    # 类型→具体模板由 settings 页 template_map 决定，两层协同 2026-08-26)
    # 2026-09-07 优化#2：同时落 projects.render_mode（迁移 34）——此后重拆分镜
    # 也按此覆写，不再只在当次批量生效
    if "render_mode" in body:
        mode = body["render_mode"]
        if mode not in ("ref2va", "fl2v", "t2v"):
            raise HTTPException(422, "render_mode 只能是 ref2va/fl2v/t2v")
        conn = db.connect()
        conn.execute("UPDATE shots SET workflow_type=? WHERE project_id=?",
                     (mode, project_id))
        conn.execute("UPDATE projects SET render_mode=? WHERE id=?",
                     (mode, project_id))
        conn.commit()

    # 漫画项目参数（迁移 36/37，2026-09-13 气泡渲染）：dialogue_mode/bubble_style
    # 等可后改——改完删对应页 →「🖼 生成缺失页」重出（提示词/后处理都吃新值）
    if any(k in body for k in ("dialogue_mode", "target_pages", "image_size",
                               "quality_tier", "bubble_style", "comic_dual_mode")):
        cur = get_project(db, project_id)
        if (cur["comic_mode"] if "comic_mode" in cur.keys() else "") != "comic_output":
            raise HTTPException(422, "仅漫画成品项目（comic_output）有漫画参数")
        vals = {}
        if "dialogue_mode" in body:
            vals["dialogue_mode"] = body["dialogue_mode"]
        if "target_pages" in body:
            try:
                vals["target_pages"] = int(body["target_pages"])
            except (TypeError, ValueError):
                raise HTTPException(422, "target_pages 需为整数")
        if "image_size" in body:
            vals["image_size"] = body["image_size"]
        if "quality_tier" in body:
            vals["quality_tier"] = body["quality_tier"]
        if "comic_dual_mode" in body:
            if body["comic_dual_mode"] not in ("stitch", "chain"):
                raise HTTPException(422, "comic_dual_mode 只能是 stitch（拼接·默认）/chain（链式逐人）")
            vals["comic_dual_mode"] = body["comic_dual_mode"]
        if vals:
            _validate_comic_output_params(
                vals.get("dialogue_mode", cur["dialogue_mode"]),
                vals.get("target_pages", cur["target_pages"]),
                vals.get("image_size", cur["image_size"]),
                vals.get("quality_tier", cur["quality_tier"]))
            conn = db.connect()
            conn.execute(
                f"UPDATE projects SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?",
                (*vals.values(), project_id))
            conn.commit()
        if "bubble_style" in body:
            raw = _validate_bubble_style(str(body["bubble_style"]))
            conn = db.connect()
            conn.execute("UPDATE projects SET bubble_style=? WHERE id=?",
                         (raw, project_id))
            conn.commit()

    # Handle era override (时代背景；检测错了可手动纠正，空串=清除)
    if "era" in body:
        conn = db.connect()
        conn.execute("UPDATE projects SET era=? WHERE id=?",
                     (str(body["era"] or "").strip(), project_id))
        conn.commit()

    # Handle video parameters (composable with style)
    if any(k in body for k in ("video_megapixels", "video_multiple", "video_speed", "default_shot_duration", "prompt_mode", "lora_realism", "target_duration", "aspect_ratio")):
        try:
            from ..engine.projects import update_video_params
            kwargs = {}
            if "aspect_ratio" in body:
                kwargs["aspect_ratio"] = body["aspect_ratio"]
            if "video_megapixels" in body:
                kwargs["video_megapixels"] = body["video_megapixels"]
            if "video_multiple" in body:
                kwargs["video_multiple"] = body["video_multiple"]
            if "video_speed" in body:
                kwargs["video_speed"] = body["video_speed"]
            if "default_shot_duration" in body:
                kwargs["default_shot_duration"] = body["default_shot_duration"]
            if "prompt_mode" in body:
                kwargs["prompt_mode"] = body["prompt_mode"]
            if "lora_realism" in body:
                kwargs["lora_realism"] = body["lora_realism"]
            if "target_duration" in body:
                kwargs["target_duration"] = body["target_duration"]
            row = update_video_params(db, project_id, **kwargs)
        except ValueError as e:
            raise HTTPException(422, str(e))

    return _public(get_project(db, project_id))
